"""Provider settings route and bounded per-browser runtime authority.

Every decision belongs to `core.provider_settings`; this module validates one closed request shape,
checks the origin and browser-session handle, and hands the intent over. Each opaque handle owns a
separate bounded runtime entry; no consent decision, credential or readiness task is process-global.

The credential crosses this boundary once, in a request body, and goes straight into a
`RuntimeCredential`. It is never echoed, never logged, never placed in an exception, and never
appears in a response: the projection carries only whether one is held. The origin and session
checks therefore apply to every intent here, not to a subset, because the same body may carry a
secret.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import TypeVar

from ..core.prompt_model_provider import (
    PromptModelContractError,
    PromptModelOutcome,
    PromptModelProfile,
)
from ..core.provider_settings import (
    ProviderExecutionSnapshot,
    ProviderIntentRejection,
    ProviderSettingsIntent,
    ProviderSettingsState,
)
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    already_owned,
    host_web_and_routes,
    register_owned_route,
    route_method_available,
)
from .composition_root import PROVIDER_SETTINGS, component

PROVIDER_SETTINGS_REQUEST_SCHEMA = "h3.context.provider_settings.request.v2"
PROVIDER_SETTINGS_ROUTE = "/h3-context/v1/provider/settings"
PROVIDER_SESSION_HEADER = "X-H3-Provider-Session"

# Finite runtime authority. These are deliberately policy constants rather than configurable
# environment values: a deployment cannot accidentally turn transient credentials into an
# unbounded or persistent store.
MAX_PROVIDER_SESSIONS = 32
PROVIDER_SESSION_IDLE_TTL_SECONDS = 30 * 60
PROVIDER_SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60

#: A credential is the largest thing that legitimately crosses here, and it is bounded to 512
#: characters by `RuntimeCredential`. Everything else is an identifier or a flag.
MAX_PROVIDER_SETTINGS_BYTES = 8_192
MAX_PROVIDER_SETTINGS_DEPTH = 3
MAX_PROVIDER_SETTINGS_ITEMS = 16

_ROOT_KEYS = {"schema", "intent", "payload"}
_PAYLOAD_KEYS = {
    "profile_id",
    "model_id",
    "credential",
    "network_permitted",
    "media_upload_consented",
}
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
_SESSION_ID = re.compile(r"ps_[0-9a-f]{32}\Z")
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_provider_settings_v1__"
_ROUTE_REGISTERED = False

_AuthorityResult = TypeVar("_AuthorityResult")


class ProviderSettingsRequestError(ValueError):
    """A closed, content-free request failure. Its code never quotes the body."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProviderSettingsSessionError(ValueError):
    """A closed session-authority failure that never quotes the supplied handle."""

    def __init__(self, code: str = "session_rejected") -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _ProviderSessionEntry:
    state: ProviderSettingsState
    created_at: float
    last_access: float
    generation: str
    readiness_task: asyncio.Task[dict[str, object]] | None = None
    assisted_cancellation: Event | None = None
    state_lock: RLock = field(default_factory=RLock, repr=False)
    pending_mutations: int = 0
    active_authority_token: object | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderSessionExecutionLease:
    """Private ownership token checked before requests, after responses and before publication."""

    session_id: str
    session_generation: str
    snapshot: ProviderExecutionSnapshot
    _registry: ProviderSettingsSessionRegistry = field(repr=False, compare=False)
    _entry: _ProviderSessionEntry = field(repr=False, compare=False)
    _signal: Event = field(repr=False, compare=False)

    def cancelled(self) -> bool:
        return self._registry._lease_cancelled(self)


@dataclass(frozen=True, slots=True)
class ProviderSessionExecutionDecision:
    outcome: PromptModelOutcome
    lease: ProviderSessionExecutionLease | None

    @property
    def admitted(self) -> bool:
        return self.lease is not None


@dataclass(frozen=True, slots=True)
class ProviderAuthorityClaim:
    """Ephemeral proof passed only while the provider authority transaction is locked."""

    session_id: str
    session_generation: str
    authority_epoch: int
    _registry: ProviderSettingsSessionRegistry = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def matches(self, session_id: str, session_generation: str, authority_epoch: int) -> bool:
        return self._registry._claim_matches(
            self,
            session_id,
            session_generation,
            authority_epoch,
        )

    def matches_lease(self, lease: object) -> bool:
        return self._registry._claim_matches_lease(self, lease)


class ProviderSettingsSessionRegistry:
    """Bounded process-memory registry for independent browser-session authority."""

    def __init__(
        self,
        *,
        state_factory: Callable[[], ProviderSettingsState] = ProviderSettingsState.from_catalog,
        clock: Callable[[], float] = time.monotonic,
        idle_ttl_seconds: float = PROVIDER_SESSION_IDLE_TTL_SECONDS,
        absolute_ttl_seconds: float = PROVIDER_SESSION_ABSOLUTE_TTL_SECONDS,
        max_sessions: int = MAX_PROVIDER_SESSIONS,
    ) -> None:
        if not callable(state_factory) or not callable(clock):
            raise TypeError("provider session dependency is invalid")
        if (
            type(idle_ttl_seconds) not in {int, float}
            or type(absolute_ttl_seconds) not in {int, float}
            or idle_ttl_seconds <= 0
            or absolute_ttl_seconds <= idle_ttl_seconds
            or type(max_sessions) is not int
            or not 1 <= max_sessions <= 128
        ):
            raise ValueError("provider session limits are invalid")
        self._state_factory = state_factory
        self._clock = clock
        self._idle_ttl = float(idle_ttl_seconds)
        self._absolute_ttl = float(absolute_ttl_seconds)
        self._max_sessions = max_sessions
        self._entries: dict[str, _ProviderSessionEntry] = {}
        self._released: dict[str, float] = {}
        self._active_authority_claims: dict[object, _ProviderSessionEntry] = {}
        self._lock = RLock()

    @staticmethod
    def validate_handle(value: object) -> str:
        if type(value) is not str or _SESSION_ID.fullmatch(value) is None:
            raise ProviderSettingsSessionError()
        return value

    @staticmethod
    def _cancel(entry: _ProviderSessionEntry) -> None:
        signal = entry.assisted_cancellation
        if signal is not None:
            signal.set()
        task = entry.readiness_task
        entry.readiness_task = None
        if task is not None and not task.done():
            task.cancel()

    def _drop(self, handle: str) -> bool:
        entry = self._entries.pop(handle, None)
        if entry is None:
            return False
        self._cancel(entry)
        return True

    def _prune(self, now: float) -> None:
        expired = [
            handle
            for handle, entry in self._entries.items()
            if now - entry.last_access >= self._idle_ttl
            or now - entry.created_at >= self._absolute_ttl
        ]
        for handle in sorted(expired):
            self._drop(handle)
        released_expired = [
            handle
            for handle, released_at in self._released.items()
            if now - released_at >= self._idle_ttl
        ]
        for handle in sorted(released_expired):
            self._released.pop(handle, None)

    def entry_for(self, handle: object) -> _ProviderSessionEntry:
        with self._lock:
            validated = self.validate_handle(handle)
            now = float(self._clock())
            self._prune(now)
            if validated in self._released:
                raise ProviderSettingsSessionError("session_released")
            entry = self._entries.get(validated)
            if entry is None:
                if len(self._entries) >= self._max_sessions:
                    victim = min(
                        self._entries,
                        key=lambda key: (
                            self._entries[key].last_access,
                            self._entries[key].created_at,
                            key,
                        ),
                    )
                    self._drop(victim)
                entry = _ProviderSessionEntry(
                    state=self._state_factory(),
                    created_at=now,
                    last_access=now,
                    generation="generation_" + secrets.token_hex(16),
                )
                self._entries[validated] = entry
            else:
                entry.last_access = now
            return entry

    def state_for(self, handle: object) -> ProviderSettingsState:
        return self.entry_for(handle).state

    def has_session(self, handle: object) -> bool:
        with self._lock:
            validated = self.validate_handle(handle)
            self._prune(float(self._clock()))
            return validated in self._entries

    @property
    def session_count(self) -> int:
        with self._lock:
            self._prune(float(self._clock()))
            return len(self._entries)

    def owns(self, handle: str, entry: _ProviderSessionEntry) -> bool:
        with self._lock:
            return self._entries.get(handle) is entry

    def dispatch_intent(
        self,
        handle: object,
        intent: ProviderSettingsIntent,
        payload: dict[str, object],
        *,
        readiness_probe: object = None,
        expected_entry: _ProviderSessionEntry | None = None,
    ) -> dict[str, object]:
        """Mutate or project one session under the same lock used by execution admission."""

        with self._lock:
            validated = self.validate_handle(handle)
            entry = self.entry_for(validated)
            if expected_entry is not None and entry is not expected_entry:
                raise ProviderSettingsSessionError("session_released")
            if intent is not ProviderSettingsIntent.READ_PROJECTION:
                # CRITICAL: cancellation, state mutation and epoch bump are one registry
                # transaction, so admission cannot combine fields from different authorities.
                self._cancel_assisted_signal(entry)
            with entry.state_lock:
                return _dispatch_entry(entry, intent, payload, readiness_probe=readiness_probe)

    def _release_pending_mutation(self, entry: _ProviderSessionEntry) -> None:
        with self._lock:
            if entry.pending_mutations > 0:
                entry.pending_mutations -= 1

    def _claim_matches(
        self,
        claim: ProviderAuthorityClaim,
        session_id: str,
        session_generation: str,
        authority_epoch: int,
    ) -> bool:
        """Validate provenance without acquiring a lock already held by the transaction."""

        entry = self._active_authority_claims.get(claim._token)
        return bool(
            claim._registry is self
            and entry is not None
            and self._entries.get(session_id) is entry
            and entry.active_authority_token is claim._token
            and claim.session_id == session_id
            and claim.session_generation == session_generation == entry.generation
            and claim.authority_epoch == authority_epoch == entry.state.authority_epoch
        )

    def _claim_matches_lease(
        self,
        claim: ProviderAuthorityClaim,
        lease: object,
    ) -> bool:
        return bool(
            isinstance(lease, ProviderSessionExecutionLease)
            and claim._registry is self is lease._registry
            and self._active_authority_claims.get(claim._token) is lease._entry
            and claim.matches(
                lease.session_id,
                lease.session_generation,
                lease.snapshot.authority_epoch,
            )
        )

    @staticmethod
    def _cancel_assisted_signal(entry: _ProviderSessionEntry) -> None:
        signal = entry.assisted_cancellation
        if signal is not None:
            signal.set()

    def run_under_assisted_authority(
        self,
        session_id: object,
        session_generation: object,
        authority_epoch: object,
        operation: Callable[[ProviderAuthorityClaim], _AuthorityResult],
        *,
        lease: ProviderSessionExecutionLease | None = None,
    ) -> _AuthorityResult:
        """Linearize proposal publication or adoption against mutation and release."""

        if (
            type(session_id) is not str
            or type(session_generation) is not str
            or type(authority_epoch) is not int
            or not callable(operation)
        ):
            raise ProviderSettingsSessionError("authority_changed")
        with self._lock:
            self._prune(float(self._clock()))
            entry = self._entries.get(session_id)
            if entry is None:
                raise ProviderSettingsSessionError("authority_changed")
            if entry.pending_mutations > 0:
                raise ProviderSettingsSessionError("authority_changed")
            with entry.state_lock:
                valid = bool(
                    entry.generation == session_generation
                    and entry.state.authority_epoch == authority_epoch
                    and session_id not in self._released
                )
                if lease is not None:
                    valid = bool(
                        valid
                        and lease._registry is self
                        and lease.session_id == session_id
                        and lease.session_generation == session_generation
                        and lease.snapshot.authority_epoch == authority_epoch
                        and lease._entry is entry
                        and entry.assisted_cancellation is lease._signal
                        and not lease._signal.is_set()
                    )
                if not valid:
                    raise ProviderSettingsSessionError("authority_changed")
                # SECURITY: provider mutation/release cannot pass this lock between the final
                # authority check and publication or canonical Accept.
                if entry.active_authority_token is not None:
                    raise ProviderSettingsSessionError("action_in_flight")
                active_claim = object()
                entry.active_authority_token = active_claim
                self._active_authority_claims[active_claim] = entry
                claim = ProviderAuthorityClaim(
                    session_id=session_id,
                    session_generation=session_generation,
                    authority_epoch=authority_epoch,
                    _registry=self,
                    _token=active_claim,
                )
                try:
                    return operation(claim)
                finally:
                    # CRITICAL: a callback cannot retain or reactivate authority after lock exit.
                    self._active_authority_claims.pop(active_claim, None)
                    if entry.active_authority_token is active_claim:
                        entry.active_authority_token = None

    def cancel_assisted_execution(self, handle: object) -> bool:
        """Signal an in-flight worker without waiting for its synchronous transport."""

        with self._lock:
            validated = self.validate_handle(handle)
            entry = self._entries.get(validated)
            signal = None if entry is None else entry.assisted_cancellation
            if signal is None:
                return False
            signal.set()
            return True

    def begin_assisted_execution(self, handle: object) -> ProviderSessionExecutionDecision:
        with self._lock:
            validated = self.validate_handle(handle)
            entry = self.entry_for(validated)
            if entry.pending_mutations > 0:
                raise ProviderSettingsSessionError("action_in_flight")
            active = entry.assisted_cancellation
            if active is not None:
                raise ProviderSettingsSessionError("action_in_flight")
            with entry.state_lock:
                decision = entry.state.execution_decision()
            if decision.snapshot is None:
                return ProviderSessionExecutionDecision(outcome=decision.outcome, lease=None)
            signal = Event()
            entry.assisted_cancellation = signal
            return ProviderSessionExecutionDecision(
                outcome=decision.outcome,
                lease=ProviderSessionExecutionLease(
                    session_id=validated,
                    session_generation=entry.generation,
                    snapshot=decision.snapshot,
                    _registry=self,
                    _entry=entry,
                    _signal=signal,
                ),
            )

    def _lease_cancelled(self, lease: ProviderSessionExecutionLease) -> bool:
        with self._lock:
            current = self._entries.get(lease.session_id)
            if current is None:
                return True
            with current.state_lock:
                authority_changed = current.state.authority_epoch != lease.snapshot.authority_epoch
            return bool(
                lease._signal.is_set()
                or current is not lease._entry
                or current.generation != lease.session_generation
                or current.assisted_cancellation is not lease._signal
                or authority_changed
            )

    def owns_authority(
        self,
        session_id: object,
        session_generation: object,
        authority_epoch: object,
    ) -> bool:
        """Recheck a stored proposal's private provider authority without reviving a session."""

        if (
            type(session_id) is not str
            or type(session_generation) is not str
            or type(authority_epoch) is not int
        ):
            return False
        with self._lock:
            self._prune(float(self._clock()))
            entry = self._entries.get(session_id)
            if entry is None:
                return False
            with entry.state_lock:
                return bool(
                    entry.generation == session_generation
                    and entry.state.authority_epoch == authority_epoch
                    and session_id not in self._released
                )

    def finish_assisted_execution(self, lease: object) -> bool:
        if not isinstance(lease, ProviderSessionExecutionLease) or lease._registry is not self:
            return False
        with self._lock:
            current = self._entries.get(lease.session_id)
            if (
                current is not lease._entry
                or current.generation != lease.session_generation
                or current.assisted_cancellation is not lease._signal
            ):
                return False
            current.assisted_cancellation = None
            return True

    def release(self, handle: object) -> bool:
        with self._lock:
            validated = self.validate_handle(handle)
            now = float(self._clock())
            self._prune(now)
            removed = self._drop(validated)
            # CRITICAL: a bounded tombstone prevents a late credential-bearing POST
            # from recreating browser authority after an explicit release.
            self._released[validated] = now
            if len(self._released) > self._max_sessions:
                victim = min(
                    self._released,
                    key=lambda key: (self._released[key], key),
                )
                self._released.pop(victim, None)
            return removed

    def clear(self) -> None:
        with self._lock:
            for handle in tuple(self._entries):
                self._drop(handle)
            self._released.clear()
            self._active_authority_claims.clear()


def build_registry() -> ProviderSettingsSessionRegistry:
    """Construct this adapter's process registry. Called only by the composition root."""

    return ProviderSettingsSessionRegistry()


_registry = component(PROVIDER_SETTINGS, ProviderSettingsSessionRegistry)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderSettingsRequestError("duplicate_member")
        result[key] = value
    return result


def _shape(value: object, *, depth: int = 0) -> None:
    if depth > MAX_PROVIDER_SETTINGS_DEPTH:
        raise ProviderSettingsRequestError("request_depth")
    if isinstance(value, dict):
        if type(value) is not dict or len(value) > MAX_PROVIDER_SETTINGS_ITEMS:
            raise ProviderSettingsRequestError("request_shape")
        for key, item in value.items():
            _shape(key, depth=depth + 1)
            _shape(item, depth=depth + 1)
        return
    if isinstance(value, list):
        raise ProviderSettingsRequestError("request_shape")
    if value is None or type(value) in {str, bool}:
        if type(value) is str and len(value) > 1_024:
            raise ProviderSettingsRequestError("request_shape")
        return
    raise ProviderSettingsRequestError("request_shape")


def decode_provider_settings_request(
    payload: bytes,
) -> tuple[ProviderSettingsIntent, dict[str, object]]:
    """Decode one closed request. Nothing outside the declared shape survives."""

    if type(payload) is not bytes or not payload:
        raise ProviderSettingsRequestError("request_empty")
    if len(payload) > MAX_PROVIDER_SETTINGS_BYTES:
        raise ProviderSettingsRequestError("request_too_large")
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderSettingsRequestError("request_malformed") from exc
    _shape(decoded)
    if type(decoded) is not dict or set(decoded) != _ROOT_KEYS:
        raise ProviderSettingsRequestError("request_shape")
    if decoded["schema"] != PROVIDER_SETTINGS_REQUEST_SCHEMA:
        raise ProviderSettingsRequestError("request_schema")
    raw_intent = decoded["intent"]
    if type(raw_intent) is not str:
        raise ProviderSettingsRequestError("request_intent")
    try:
        intent = ProviderSettingsIntent(raw_intent)
    except ValueError as exc:
        raise ProviderSettingsRequestError("request_intent") from exc
    raw_payload = decoded["payload"]
    if raw_payload is None:
        raw_payload = {}
    if type(raw_payload) is not dict or not set(raw_payload) <= _PAYLOAD_KEYS:
        raise ProviderSettingsRequestError("request_payload")
    profile_id = raw_payload.get("profile_id")
    if profile_id is not None and (
        type(profile_id) is not str or _PROFILE_ID.fullmatch(profile_id) is None
    ):
        raise ProviderSettingsRequestError("request_payload")
    model_id = raw_payload.get("model_id")
    if model_id is not None and (
        type(model_id) is not str or _MODEL_ID.fullmatch(model_id) is None or ".." in model_id
    ):
        raise ProviderSettingsRequestError("request_payload")
    for flag in ("network_permitted", "media_upload_consented"):
        value = raw_payload.get(flag)
        if value is not None and type(value) is not bool:
            raise ProviderSettingsRequestError("request_payload")
    credential = raw_payload.get("credential")
    if credential is not None and type(credential) is not str:
        # The value itself is not inspected further here: `RuntimeCredential` owns that rule, and
        # duplicating it would mean two places could disagree about what a credential may be.
        raise ProviderSettingsRequestError("request_payload")
    return intent, dict(raw_payload)


def provider_settings_state(
    session_id: object,
    *,
    registry: ProviderSettingsSessionRegistry | None = None,
) -> ProviderSettingsState:
    """Resolve one browser-owned state, built from the shipped catalog on first access."""

    return (_registry() if registry is None else registry).state_for(session_id)


def provider_settings_registry() -> ProviderSettingsSessionRegistry:
    """Return the process-local authority owner for another in-package adapter."""

    return _registry()


def provider_session_id_from_request(request: object) -> str:
    """Apply the same exact single-header rule before another route reads a private body."""

    return _request_session_id(request)


def reset_provider_settings_state() -> None:
    """Drop all runtime entries for tests/host teardown, including pending readiness."""

    _registry().clear()


def _dispatch_entry(
    entry: _ProviderSessionEntry,
    intent: ProviderSettingsIntent,
    payload: dict[str, object],
    *,
    readiness_probe: object = None,
) -> dict[str, object]:
    try:
        result = entry.state.apply(intent, payload, readiness_probe=readiness_probe)
    except PromptModelContractError as exc:
        raise ProviderSettingsRequestError(exc.code) from None
    return result.to_wire()


def _dispatch_readiness_entry(
    entry: _ProviderSessionEntry,
    intent: ProviderSettingsIntent,
    payload: dict[str, object],
    *,
    readiness_probe: object,
) -> dict[str, object]:
    """Run a blocking probe under only this session's state lock."""

    with entry.state_lock:
        return _dispatch_entry(entry, intent, payload, readiness_probe=readiness_probe)


def dispatch_provider_settings(
    session_id: object,
    intent: ProviderSettingsIntent,
    payload: dict[str, object],
    *,
    readiness_probe: object = None,
    registry: ProviderSettingsSessionRegistry | None = None,
) -> dict[str, object]:
    """Apply one intent and return the wire result. Never returns the submitted credential."""

    registry = _registry() if registry is None else registry
    return registry.dispatch_intent(
        session_id,
        intent,
        payload,
        readiness_probe=readiness_probe,
    )


def _probe_provider_readiness(
    state: ProviderSettingsState,
    profile: object,
    credential: object = None,
) -> object:
    """Load the socket-owning adapter only for an explicit readiness request."""

    from .provider_readiness import probe_provider_readiness

    pinned = (
        tuple(item.model_id for item in state.profiles if item.family is profile.family)
        if isinstance(profile, PromptModelProfile)
        else ()
    )
    return probe_provider_readiness(profile, credential, pinned_identifiers=pinned)


async def dispatch_provider_settings_async(
    session_id: object,
    intent: ProviderSettingsIntent,
    payload: dict[str, object],
    *,
    readiness_probe: object = None,
    registry: ProviderSettingsSessionRegistry | None = None,
) -> dict[str, object]:
    """Serialize only one session and share no readiness work across browser authority."""

    registry = _registry() if registry is None else registry
    handle = registry.validate_handle(session_id)
    reserved_mutation = False
    with registry._lock:
        entry = registry.entry_for(handle)
        if intent is not ProviderSettingsIntent.READ_PROJECTION:
            registry._cancel_assisted_signal(entry)
        active = entry.readiness_task
        if active is not None and active.done():
            entry.readiness_task = None
            active = None

        if intent not in {
            ProviderSettingsIntent.READ_PROJECTION,
            ProviderSettingsIntent.RECHECK_READINESS,
        }:
            entry.pending_mutations += 1
            reserved_mutation = True

        if intent is ProviderSettingsIntent.RECHECK_READINESS and active is None:
            probe = (
                (
                    lambda profile, credential: _probe_provider_readiness(
                        entry.state, profile, credential
                    )
                )
                if readiness_probe is None
                else readiness_probe
            )
            # IMPORTANT: reserve mutation authority before yielding. Admission and proposal
            # publication observe the reservation while the worker owns only this session state.
            entry.pending_mutations += 1
            try:
                active = asyncio.create_task(
                    asyncio.to_thread(
                        _dispatch_readiness_entry,
                        entry,
                        intent,
                        payload,
                        readiness_probe=probe,
                    )
                )
            except Exception:
                entry.pending_mutations -= 1
                raise
            active.add_done_callback(lambda _task: registry._release_pending_mutation(entry))
            entry.readiness_task = active

    if intent is ProviderSettingsIntent.RECHECK_READINESS:
        if active is None:
            raise ProviderSettingsSessionError("session_released")
        try:
            # One disconnected browser must not cancel the request shared by another caller.
            wire = await asyncio.shield(active)
            with registry._lock:
                if not registry.owns(handle, entry):
                    raise ProviderSettingsSessionError("session_released")
                return wire
        finally:
            with registry._lock:
                if active.done() and entry.readiness_task is active:
                    entry.readiness_task = None

    try:
        if active is not None:
            # State used to admit the request cannot change while that request is in flight.
            await asyncio.shield(active)
            with registry._lock:
                if active.done() and entry.readiness_task is active:
                    entry.readiness_task = None
        return registry.dispatch_intent(handle, intent, payload, expected_entry=entry)
    finally:
        if reserved_mutation:
            registry._release_pending_mutation(entry)


_REJECTION_STATUS = {
    ProviderIntentRejection.UNKNOWN_INTENT: 400,
    ProviderIntentRejection.UNKNOWN_PROFILE: 404,
    ProviderIntentRejection.UNKNOWN_MODEL: 404,
    ProviderIntentRejection.NO_SELECTION: 409,
    ProviderIntentRejection.NO_MODEL_SELECTION: 409,
    ProviderIntentRejection.CONSENT_NOT_APPLICABLE: 409,
    ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE: 409,
    ProviderIntentRejection.CREDENTIAL_REJECTED: 422,
    ProviderIntentRejection.CATALOG_EMPTY: 409,
}


def _request_session_id(request: object) -> str:
    headers = getattr(request, "headers", None)
    getall = getattr(headers, "getall", None)
    values = getall(PROVIDER_SESSION_HEADER, []) if callable(getall) else []
    if type(values) is not list or len(values) != 1:
        raise ProviderSettingsSessionError()
    return ProviderSettingsSessionRegistry.validate_handle(values[0])


_POST_POLICY = RoutePolicy(
    path=PROVIDER_SETTINGS_ROUTE,
    owner=PROVIDER_SETTINGS_REQUEST_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_PROVIDER_SETTINGS_BYTES,
    refusals=(ProviderSettingsRequestError, ProviderSettingsSessionError),
)

_DELETE_POLICY = RoutePolicy(
    path=PROVIDER_SETTINGS_ROUTE,
    owner=PROVIDER_SETTINGS_REQUEST_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="DELETE",
    refusals=(ProviderSettingsSessionError,),
)


def _refusal_body(status: int, reason: str) -> dict[str, str]:
    # This pair has always answered an unsupported media type with the generic code rather than a
    # media-specific one; every other seam-level refusal already used the seam's own reason.
    return {"error": "invalid_request" if status == 415 else reason}


def _session_prelude(request: object) -> object:
    """Resolve the session handle before a single body byte is read.

    CRITICAL: this body may carry a provider credential, so the caller's ownership of a session is
    settled before this process materializes anything it sent. Moving this after the body read
    would let an unauthenticated caller spend the route's whole byte budget.
    """

    try:
        return _request_session_id(request)
    except ProviderSettingsSessionError as exc:
        return RouteResult(400, {"error": exc.code})


def _refuse(error: BaseException) -> RouteResult:
    code = getattr(error, "code", None)
    reason = code if isinstance(code, str) else "invalid_request"
    if type(error) is ProviderSettingsRequestError:
        return RouteResult(413 if reason == "request_too_large" else 400, {"error": reason})
    # A session refusal raised past the prelude means the authority the handle named is gone, which
    # is a different answer from the prelude's "this handle is not one of ours".
    return RouteResult(410, {"error": reason})


def _rejection_status(rejection: str) -> int:
    """Map a rejection this process produced onto its wire status.

    CRITICAL: `rejection` comes from this package's own dispatch, never from the caller, so a value
    outside the enum is this process's bug. `ProviderIntentRejection(rejection)` raises `ValueError`
    for one, and `ValueError` is what the seam reads as "the caller sent something undecodable" --
    it would answer 400 and tell a caller whose request was correct to change it. Re-raising outside
    that trio reaches the seam's blanket guard, which answers 500 without leaking the value.
    """

    try:
        known = ProviderIntentRejection(rejection)
    except ValueError as error:
        raise RuntimeError("provider dispatch produced an unknown rejection") from error
    return _REJECTION_STATUS.get(known, 400)


async def _apply(payload: bytes, context: object) -> RouteResult:
    intent, action = decode_provider_settings_request(payload)
    wire = await dispatch_provider_settings_async(str(context), intent, action)
    rejection = wire.get("rejection")
    if not wire.get("accepted") and isinstance(rejection, str):
        return RouteResult(_rejection_status(rejection), wire)
    return RouteResult(200, wire)


async def _release(_payload: bytes, context: object) -> RouteResult:
    _registry().release(str(context))
    return RouteResult(204)


def ensure_provider_settings_route_registered() -> bool:
    """Register the route while keeping host imports out of package requirements."""

    global _ROUTE_REGISTERED
    found = host_web_and_routes()
    if found is None:
        return False
    _, routes = found
    # CRITICAL: both halves of the pair are inspected -- for a foreign handler and for the host
    # router's support of the method -- before either one is registered. Either refusal must leave
    # the other half unregistered too; claiming one half and reporting failure would leave a route
    # installed that nothing here owns or can remove. `register_owned_route` performs the same two
    # checks itself, but it performs them one route at a time, which is too late for a pair.
    for policy in (_POST_POLICY, _DELETE_POLICY):
        if not route_method_available(routes, policy):
            _ROUTE_REGISTERED = False
            return False
        if already_owned(routes, policy, __name__) is False:
            _ROUTE_REGISTERED = False
            return False
    post = register_owned_route(
        _POST_POLICY,
        __name__,
        _apply,
        _refusal_body,
        refusal_mapper=_refuse,
        prelude=_session_prelude,
    )
    release = register_owned_route(
        _DELETE_POLICY,
        __name__,
        _release,
        _refusal_body,
        refusal_mapper=_refuse,
        prelude=_session_prelude,
    )
    _ROUTE_REGISTERED = post and release
    return _ROUTE_REGISTERED


__all__ = [
    "MAX_PROVIDER_SETTINGS_BYTES",
    "MAX_PROVIDER_SETTINGS_DEPTH",
    "MAX_PROVIDER_SETTINGS_ITEMS",
    "MAX_PROVIDER_SESSIONS",
    "PROVIDER_SETTINGS_REQUEST_SCHEMA",
    "PROVIDER_SETTINGS_ROUTE",
    "PROVIDER_SESSION_ABSOLUTE_TTL_SECONDS",
    "PROVIDER_SESSION_HEADER",
    "PROVIDER_SESSION_IDLE_TTL_SECONDS",
    "ProviderSettingsRequestError",
    "ProviderSettingsSessionError",
    "ProviderSessionExecutionDecision",
    "ProviderSessionExecutionLease",
    "ProviderSettingsSessionRegistry",
    "decode_provider_settings_request",
    "dispatch_provider_settings",
    "dispatch_provider_settings_async",
    "ensure_provider_settings_route_registered",
    "provider_settings_state",
    "provider_settings_registry",
    "provider_session_id_from_request",
    "reset_provider_settings_state",
]
