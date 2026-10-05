"""Readiness probe: the one place that asks a provider whether it is there.

This module is deliberately the smallest observer possible: a single `GET`, no retry, no session,
no prompt text, and nothing written anywhere.

Three boundaries are worth stating because they are easy to erode later.

**The decision to ask is not made here.** `ProviderSettingsState.recheck` runs the consent gate,
and the product route reaches this adapter only through that injected callback. This file does not
re-implement the decision; it still refuses to build a credentialed exchange without a credential.

**A failure to reach is not a failure to ask, and neither is an answer that cannot be read.** An
error raised before any response arrived becomes `reachable=False` with the transport's own
identity. An error raised about a response that did arrive becomes `reachable=True` carrying that
same identity, because the provider answered even though its answer was refused. A refusal to
transmit never gets here at all, so it can never be misreported as a silent host.

**The census is the same request.** The identity call already returns the runtime's model list, so
the scan detail is read out of that response rather than costing a second round trip, and the pure
`describe_discovery` does the judging.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from time import monotonic

from ..core.contracts import ValidationSeverity
from ..core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    PromptModelContractError,
    PromptModelEgressError,
    PromptModelFamily,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelQualificationState,
    QualifiedIdentityObservation,
    RemotePromptModelQualificationEvidence,
    admit_egress_destination,
    build_prompt_model_outcome,
    compute_endpoint_fingerprint,
    match_qualification_identity,
    parse_exact_tags_row,
    remediation_for,
)
from ..core.prompt_model_session import (
    DiscoveryCandidate,
    DiscoveryObservation,
    LiveModelIdentity,
    describe_discovery,
)
from ..core.provider_settings import ReadinessObservation
from ..core.remote_prompt_model import REMOTE_FAMILIES, RuntimeCredential
from ..core.remote_provider_policy import policy_for_profile
from .prompt_model_transport import (
    LoopbackJsonExchange,
    PromptModelTransportError,
    RemoteHttpsExchange,
    request_spelling,
    resolve_live_identity,
    resolve_pinned_address_bounded,
    resolve_show_identity,
)

#: A readiness check is a user waiting on a spinner, not a drafting request. It gets a short
#: budget of its own rather than the session timeouts, and it never retries: a second automatic
#: request to a third party is a second transmission the user did not ask for.
LOOPBACK_PROBE_TIMEOUT_SECONDS = 5.0
REMOTE_PROBE_TIMEOUT_SECONDS = 10.0

_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


def _failed(outcome_id: PromptModelOutcomeId) -> ReadinessObservation:
    """A provider that could not be reached. No provider prose crosses this boundary."""

    return ReadinessObservation(
        reachable=False,
        outcome=build_prompt_model_outcome(
            outcome_id,
            severity=ValidationSeverity.ERROR,
            remediation=remediation_for(outcome_id),
            parameters=(),
        ),
    )


def _answered(
    outcome_id: PromptModelOutcomeId | None,
    candidates: tuple[DiscoveryCandidate, ...],
    identity: QualifiedIdentityObservation | None = None,
) -> ReadinessObservation:
    """A provider that answered. It is reachable whether or not it holds the pinned model."""

    outcome = (
        None
        if outcome_id is None
        else build_prompt_model_outcome(
            outcome_id,
            severity=ValidationSeverity.ERROR,
            remediation=remediation_for(outcome_id),
            parameters=(),
        )
    )
    return ReadinessObservation(
        reachable=True, outcome=outcome, candidates=candidates, identity=identity
    )


def _prove_qualified_identity(
    profile: PromptModelProfile, listing: object, exchange: object
) -> QualifiedIdentityObservation | PromptModelOutcomeId:
    """Prove the exact qualified identity using the evidence kind's truthful identity basis.

    Ollama spends the second request comparing digest/show facts. A remote provider publishes an
    alias, not a weight digest, so its separately authorized live evidence is policy-bound and the
    exact discovery alias already observed by the first request is the complete recheck.

    Returns the observation on agreement and a closed outcome id on any disagreement, so a caller
    can report *reachable but not ready* without having to invent a reason.
    """

    evidence = profile.qualification_evidence
    qualified = profile.qualification_state is PromptModelQualificationState.QUALIFIED
    if evidence is None or not qualified:
        return PromptModelOutcomeId.CAPABILITY_MISMATCH
    if isinstance(evidence, RemotePromptModelQualificationEvidence):
        # This repeats the policy/evidence equality check even for an injected test exchange. A
        # validly shaped but stale live artifact is not authority for the current route or price.
        policy_for_profile(profile)
        return QualifiedIdentityObservation(
            profile_id=profile.profile_id,
            model_id=profile.model_id,
            model_digest=None,
            qualification_sha256=evidence.qualification_sha256,
            endpoint_sha256=compute_endpoint_fingerprint(profile.endpoint),
            observed_at=monotonic(),
        )
    tags = parse_exact_tags_row(listing, profile.model_id)
    show = resolve_show_identity(profile.model_id, exchange)
    mismatch = match_qualification_identity(evidence, tags, show)
    if mismatch is not None:
        return mismatch
    return QualifiedIdentityObservation(
        profile_id=profile.profile_id,
        model_id=profile.model_id,
        model_digest=tags.model_digest,
        qualification_sha256=evidence.show_identity_sha256,
        endpoint_sha256=compute_endpoint_fingerprint(profile.endpoint),
        # A monotonic reading, not a wall clock: the only question ever asked of it is how long ago,
        # and a wall clock would put the host's date into a value that gets projected to a browser.
        observed_at=monotonic(),
    )


def _census(
    family: PromptModelFamily,
    listing: object,
    pinned: frozenset[str],
    *,
    discovery_route: str | None = None,
) -> tuple[DiscoveryCandidate, ...]:
    """Turn the listing the identity call already fetched into judged candidates.

    Reading a provider's response is reading untrusted input. One malformed row invalidates the
    whole census: retaining only the rows that happen to parse would turn a partial answer into
    apparent authority.
    """

    if not isinstance(listing, Mapping):
        raise PromptModelContractError("readiness_census")
    entries = listing.get("models") if family is PromptModelFamily.OLLAMA else listing.get("data")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise PromptModelContractError("readiness_census")
    # SECURITY: bound the raw provider array before examining or filtering any row. Otherwise an
    # attacker can hide an oversized answer behind entries the old parser silently skipped.
    if len(entries) > MAX_DISCOVERY_ROWS:
        raise PromptModelContractError("readiness_census")
    identifiers: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise PromptModelContractError("readiness_census")
        raw = (
            entry.get("name") or entry.get("model")
            if family is PromptModelFamily.OLLAMA
            else entry.get("id")
        )
        if not isinstance(raw, str) or _MODEL_IDENTIFIER.fullmatch(raw) is None or ".." in raw:
            raise PromptModelContractError("readiness_census")
        # M22-17. Validate the wire value exactly as received, then join it to the spelling the
        # profile pins. A provider whose listing route answers `models/<id>` while its chat route
        # takes the bare id would otherwise have every row judged UNPINNED, and the census would
        # report the selected model as absent from its own catalogue.
        identifiers.append(request_spelling(raw, discovery_route))
    # SECURITY: ambiguity is a property of the identifier, not of a row's position. Flagging only
    # the second occurrence left the first one ADMITTED, so of two rows naming one identity one
    # passed and one was refused, and which it was depended on listing order. Every occurrence of a
    # repeated identifier is refused, so this census fails closed on its own rather than by relying
    # on a caller checking something else first.
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in identifiers:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    observations: list[DiscoveryObservation] = [
        DiscoveryObservation(
            identifier=value, root_present=True, duplicate_identifier=value in repeated
        )
        for value in identifiers
    ]
    if not observations:
        return ()
    return describe_discovery(observations, pinned)


class _RecordingExchange:
    """Delegates every call and remembers the last response.

    The census and the identity check want the same listing, and asking for it twice would be two
    requests where the user asked for one -- and, for the remote family, two transmissions where
    consent was given for one. Wrapping the exchange keeps `resolve_live_identity` untouched and
    makes "the census is the same request" literally true rather than nearly true.
    """

    __slots__ = ("_inner", "listing")

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.listing: object = None

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        response: Mapping[str, object] = self._inner.request(  # type: ignore[attr-defined]
            method, path, payload, timeout_seconds=timeout_seconds
        )
        self.listing = response
        return response


def probe_provider_readiness(
    profile: object,
    credential: object = None,
    *,
    pinned_identifiers: object = (),
    exchange_factory: object = None,
) -> ReadinessObservation:
    """Ask one provider what it is holding, once.

    `exchange_factory` exists for tests and takes `(destination, credential)`. Production passes
    nothing and gets the accepted transports.
    """

    if not isinstance(profile, PromptModelProfile):
        return _failed(PromptModelOutcomeId.CAPABILITY_MISMATCH)
    supplied = (
        tuple(pinned_identifiers)
        if isinstance(pinned_identifiers, (tuple, list, frozenset, set))
        else ()
    )
    pinned = frozenset(str(value) for value in (supplied or (profile.model_id,)))
    try:
        destination = admit_egress_destination(profile.family, profile.endpoint)
    except PromptModelEgressError:
        # The endpoint the catalog pins is not one this build will talk to. That is a
        # configuration fact, and `CORRECT_ENDPOINT` is what an operator can do about it.
        return _failed(PromptModelOutcomeId.EGRESS_REFUSED)

    # M22-20. Whether the provider ever answered has to be observable from the handler below, so
    # the recorder is bound before the block that can fail. A recorder that has heard nothing
    # carries `listing is None`, which is exactly the state "no response was received".
    recorder = _RecordingExchange(None)
    try:
        if exchange_factory is not None:
            if not callable(exchange_factory):
                return _failed(PromptModelOutcomeId.TRANSPORT)
            exchange = exchange_factory(destination, credential)
        elif profile.family in REMOTE_FAMILIES:
            if not isinstance(credential, RuntimeCredential):
                # Unreachable through `recheck`, whose consent gate refuses first. Kept because
                # this function is the last thing between a profile and a TLS socket.
                return _failed(PromptModelOutcomeId.AUTHENTICATION)
            started = monotonic()
            pinned_address = resolve_pinned_address_bounded(
                destination.host,
                timeout_seconds=REMOTE_PROBE_TIMEOUT_SECONDS,
                loopback_required=False,
            )
            remaining = REMOTE_PROBE_TIMEOUT_SECONDS - (monotonic() - started)
            if remaining <= 0:
                raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, "")
            exchange = RemoteHttpsExchange(
                destination,
                credential,
                policy=policy_for_profile(profile),
                timeout_seconds=remaining,
                address_resolver=lambda _host: pinned_address,
            )
        else:
            started = monotonic()
            pinned_address = resolve_pinned_address_bounded(
                destination.host,
                timeout_seconds=LOOPBACK_PROBE_TIMEOUT_SECONDS,
                loopback_required=True,
            )
            remaining = LOOPBACK_PROBE_TIMEOUT_SECONDS - (monotonic() - started)
            if remaining <= 0:
                raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, "")
            exchange = LoopbackJsonExchange(
                destination,
                timeout_seconds=remaining,
                address_resolver=lambda _host: pinned_address,
            )
        recorder = _RecordingExchange(exchange)
        # Both readers of this one listing must be handed the same route, or they could disagree
        # about what spelling the provider answers with.
        discovery_route = profile.discovery_routes[0] if profile.discovery_routes else None
        identity = resolve_live_identity(
            profile.family,
            profile.model_id,
            recorder,
            discovery_route=discovery_route,
        )
    except PromptModelTransportError as error:
        # M22-20. `_failed` says the provider could not be reached, and that is not what these
        # errors mean once a body has already come back. Every `MALFORMED_RESPONSE` raised inside
        # `resolve_live_identity` is a judgement about a response that was received and parsed: a
        # listing longer than the ceiling, a row that will not read, a digest that will not
        # normalize, two rows claiming one identity. The socket connected, the request went out,
        # and the provider answered -- badly. Telling the user it could not be reached sends them
        # to look at their network for a problem that is in the answer.
        #
        # The recorder is the discriminator rather than the outcome id, because the exchange itself
        # can raise `MALFORMED_RESPONSE` for a body it could not decode, and that case is pinned at
        # `reachable=False` by an M22-15 test. The recorder answers the question actually being
        # asked -- did the provider answer -- instead of a proxy for it. The outcome id is carried
        # through rather than relabelled, so a future outcome raised there is reported faithfully.
        #
        # No subset of a listing the identity reader refused is selection authority, which is why
        # the candidates are empty here for the same reason the census path beside it says so.
        if recorder.listing is None:
            return _failed(error.outcome_id)
        return _answered(error.outcome_id, ())

    try:
        candidates = _census(
            profile.family, recorder.listing, pinned, discovery_route=discovery_route
        )
    except PromptModelContractError:
        # The socket answered, but no subset of a malformed census is selection authority.
        return _answered(PromptModelOutcomeId.MALFORMED_RESPONSE, ())
    if identity is None:
        # The provider answered and does not hold the pinned model. Reachable, not usable.
        return _answered(PromptModelOutcomeId.MODEL_MISSING, candidates)
    if not isinstance(identity, LiveModelIdentity):
        return _failed(PromptModelOutcomeId.MALFORMED_RESPONSE)
    if (
        profile.model_digest
        and identity.digest is not None
        and identity.digest != profile.model_digest
    ):
        # A digest the runtime publishes and that differs from the pin is the one case where a
        # reachable provider must not be called ready: the weights are not the ones pinned.
        return _answered(PromptModelOutcomeId.DIGEST_MISMATCH, candidates)
    if profile.qualification_state is not PromptModelQualificationState.QUALIFIED:
        # Nothing was qualified, so nothing is proved, and the observation says so by carrying no
        # identity. This is the path every catalog-only profile takes.
        return _answered(None, candidates)
    try:
        proof = _prove_qualified_identity(profile, recorder.listing, exchange)
    except PromptModelTransportError as error:
        return _failed(error.outcome_id)
    except PromptModelContractError:
        # The listing or the show response could not be read strictly. The provider is reachable
        # and its answer is not authority for anything.
        return _answered(PromptModelOutcomeId.MALFORMED_RESPONSE, candidates)
    if isinstance(proof, PromptModelOutcomeId):
        return _answered(proof, candidates)
    return _answered(None, candidates, proof)


__all__ = [
    "LOOPBACK_PROBE_TIMEOUT_SECONDS",
    "REMOTE_PROBE_TIMEOUT_SECONDS",
    "probe_provider_readiness",
]
