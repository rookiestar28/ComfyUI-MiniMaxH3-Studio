"""M22-06 provider settings: the canonical state the Settings surface renders and issues intents at.

The browser owns none of this. It renders an allowlisted projection and sends intents; readiness is
decided here, the disclosure is composed here from typed facts, and the consent ledger and the
session credential live here. That split is what keeps a presentation change from being able to
weaken a consent decision.

The disclosure is composed from facts, never from a sentence. `ProviderSetup.disclosure()` returns
one English line built for an evidence log; a surface that has to be independently verifiable in
three locales cannot be built by translating it. What a request transmits is a property of the
`M22-01` route row for the family and of the pinned profile's declared capabilities, so those are
what the projection carries, and each locale renders the same facts rather than a translation of a
paraphrase of them.

Nothing here is persisted. A consent decision and a credential last for the session that made them,
which is `M22-04`'s design and not an omission -- and because that is surprising, the projection
says so, in every locale, wherever a grant control is offered.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from hashlib import sha256
from time import monotonic
from typing import NoReturn

from .assisted_authoring_scope import AssistedAuthoringState
from .contracts import ValidationSeverity
from .prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    AdmittedDestination,
    LegacyPromptModelProfile,
    ModelChoice,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelEgressError,
    PromptModelFamily,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelQualificationState,
    PromptModelRemediation,
    PromptModelRoute,
    ProviderReadinessEvidence,
    QualifiedIdentityObservation,
    admit_egress_destination,
    build_prompt_model_outcome,
    compute_endpoint_fingerprint,
    load_prompt_model_catalog,
    route_for_family,
    stamp_readiness_evidence,
)
from .prompt_model_session import DiscoveryCandidate
from .provider_setup import ProviderConsentStatus
from .remote_prompt_model import (
    REMOTE_FAMILIES,
    REMOTE_FAMILY,
    RemoteConsentLedger,
    RemoteConsentRecord,
    RuntimeCredential,
    admit_remote_transmission,
)
from .remote_provider_policy import (
    policy_for_profile,
    remote_provider_policies,
)

PROVIDER_SETTINGS_SCHEMA = "h3.context.provider_settings.v3"

#: The accepted census is already bounded at the provider boundary. Project every accepted row,
#: so that what the surface shows is the whole of what session state accepted.
#: This derivation is the safety property, not a convenience, and must never be replaced by a
#: literal. A smaller window would not weaken authority -- exact selection scans the unsliced
#: `_candidates`, so a row past the window is still judged -- and that is precisely why it is
#: dangerous: rows the provider sent and the session accepted would be judged while never being
#: rendered, so the user could neither see nor audit the data a decision was made from. That is
#: the defect `test_an_exact_candidate_after_the_old_ui_window_remains_selectable` was written
#: for. M22-19 raised the ingest ceiling to 512 and this window rose with it, deliberately.
MAX_PROJECTED_CANDIDATES = MAX_DISCOVERY_ROWS

#: A consent decision and a credential are session-scoped by construction. This value is projected
#: so the surface states the scope rather than leaving the user to assume persistence.
CONSENT_SCOPE = "session_only"


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


class ProviderReadiness(str, Enum):
    """The four states a provider can be in. A spinner is not one of them.

    Each unready state names a different next action, which is the whole reason they are separate:
    "you have not chosen one yet" and "the one you chose is not answering" are not the same problem
    and do not have the same fix.
    """

    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"
    INCOMPATIBLE = "incompatible"
    READY = "ready"


class ProviderSettingsIntent(str, Enum):
    """Every effect this surface may ask for. A control outside this set has no backend effect."""

    READ_PROJECTION = "read_projection"
    SELECT_PROFILE = "select_profile"
    CLEAR_SELECTION = "clear_selection"
    SELECT_MODEL = "select_model"
    CONNECT_AND_REFRESH = "connect_and_refresh"
    CLEAR_MODEL = "clear_model"
    SUBMIT_CREDENTIAL = "submit_credential"
    DISCARD_CREDENTIAL = "discard_credential"
    GRANT_CONSENT = "grant_consent"
    REVOKE_CONSENT = "revoke_consent"
    RECHECK_READINESS = "recheck_readiness"


class ProviderIntentRejection(str, Enum):
    """Why an intent was refused, as a closed identity the surface maps to its own copy."""

    UNKNOWN_INTENT = "unknown_intent"
    UNKNOWN_PROFILE = "unknown_profile"
    UNKNOWN_MODEL = "unknown_model"
    NO_SELECTION = "no_selection"
    NO_MODEL_SELECTION = "no_model_selection"
    CONSENT_NOT_APPLICABLE = "consent_not_applicable"
    CREDENTIAL_NOT_APPLICABLE = "credential_not_applicable"
    CREDENTIAL_REJECTED = "credential_rejected"
    CATALOG_EMPTY = "catalog_empty"
    STALE_REVISION = "stale_revision"
    CONSENT_REQUIRED = "consent_required"


@dataclass(frozen=True, slots=True)
class TransmissionDisclosure:
    """What a request to the selected provider transmits, as facts rather than as a sentence.

    Every field here is either an accepted enum value or a boolean. There is no prose, so a locale
    cannot understate the transfer boundary by choosing a softer word -- there is no word to choose.
    """

    family: PromptModelFamily
    destination: str
    transfer_boundary: str
    preflight_required: bool
    consent_required: bool
    local_only: bool
    requires_credential: bool
    accepted_media: tuple[str, ...]
    transmits_media: bool
    consent_scope: str = CONSENT_SCOPE
    provider_id: str = ""
    retention_policy: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "destination": self.destination,
            "transfer_boundary": self.transfer_boundary,
            "preflight_required": self.preflight_required,
            "consent_required": self.consent_required,
            "local_only": self.local_only,
            "requires_credential": self.requires_credential,
            "accepted_media": list(self.accepted_media),
            "transmits_media": self.transmits_media,
            "consent_scope": self.consent_scope,
            "provider_id": self.provider_id,
            "retention_policy": self.retention_policy,
        }


def describe_transmission(
    route: object,
    capabilities: object,
    *,
    media_attached: object = False,
    profile: PromptModelProfile | None = None,
) -> TransmissionDisclosure:
    """Compose the disclosure from the route row and the profile's declared capabilities."""

    if not isinstance(route, PromptModelRoute):
        _fail("disclosure_route")
    if not isinstance(capabilities, PromptModelCapabilities):
        _fail("disclosure_capabilities")
    if type(media_attached) is not bool:
        _fail("disclosure_media_flag")
    if capabilities.family is not route.family:
        _fail("disclosure_family_mismatch")
    media = tuple(sorted(item.value for item in capabilities.accepted_media))
    if profile is not None and not isinstance(profile, PromptModelProfile):
        _fail("disclosure_profile")
    policy = None
    if route.family in REMOTE_FAMILIES:
        if profile is not None:
            try:
                policy = policy_for_profile(profile)
            except PromptModelContractError:
                if any(
                    item.profile_id == profile.profile_id for item in remote_provider_policies()
                ):
                    raise
    return TransmissionDisclosure(
        family=route.family,
        destination=route.destination.value,
        transfer_boundary=route.transfer_boundary.value,
        preflight_required=route.preflight_required,
        consent_required=route.consent_required,
        local_only=route.local_only,
        requires_credential=capabilities.requires_credential,
        accepted_media=media,
        # A family that accepts only text cannot transmit media whatever the user attaches.
        transmits_media=media_attached and media != ("text",),
        provider_id="" if policy is None else policy.provider_id,
        retention_policy="" if profile is None else profile.retention_policy,
    )


@dataclass(frozen=True, slots=True)
class ProviderProfileView:
    """One selectable profile, as identity. Every field is rendered verbatim in every locale."""

    profile_id: str
    provider_label: str
    family: PromptModelFamily
    wire_dialect: str
    adapter_version: str
    parser_version: str
    cost_class: str
    usage_receipt_required: bool
    retention_policy: str
    qualification_state: str
    limitations: tuple[str, ...]
    host: str
    port: int

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "provider_label": self.provider_label,
            "family": self.family.value,
            "wire_dialect": self.wire_dialect,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "cost_class": self.cost_class,
            "usage_receipt_required": self.usage_receipt_required,
            "retention_policy": self.retention_policy,
            "qualification_state": self.qualification_state,
            "limitations": list(self.limitations),
            "host": self.host,
            "port": self.port,
        }


def _destination_of(profile: PromptModelProfile) -> AdmittedDestination | None:
    """An in-process family has no endpoint, and a bad one must not break the whole surface."""

    route = route_for_family(profile.family)
    if route.destination.value == "in_process":
        return None
    try:
        return admit_egress_destination(profile.family, profile.endpoint)
    except PromptModelEgressError:
        return None


def view_of_profile(profile: object) -> ProviderProfileView:
    """Project one profile. The endpoint is reduced to host and port by the chokepoint.

    A profile's endpoint never reaches the browser as a string: it goes through
    `admit_egress_destination` first, which is what strips any query or embedded credential a
    hand-edited catalog might carry, and refuses a destination the family may not reach at all.
    """

    if not isinstance(profile, PromptModelProfile):
        _fail("profile_view")
    destination = _destination_of(profile)
    return ProviderProfileView(
        profile_id=profile.profile_id,
        provider_label=profile.provider_label,
        family=profile.family,
        wire_dialect=profile.wire_dialect.value,
        adapter_version=profile.adapter_version,
        parser_version=profile.parser_version,
        cost_class=profile.cost_class,
        usage_receipt_required=profile.usage_receipt_required,
        retention_policy=profile.retention_policy,
        qualification_state=profile.qualification_state.value,
        limitations=profile.limitations,
        host="" if destination is None else destination.host,
        port=0 if destination is None else destination.port,
    )


@dataclass(frozen=True, slots=True)
class ProviderConsentView:
    """The consent decision as the surface sees it. Never a credential, only that one exists."""

    profile_id: str
    status: str
    network_permitted: bool
    media_upload_consented: bool
    revision: int
    scope: str = CONSENT_SCOPE

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "network_permitted": self.network_permitted,
            "media_upload_consented": self.media_upload_consented,
            "revision": self.revision,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """A typed failure and, where one exists, the identity of its single remediation.

    The remediation is named by the outcome identity, never by its message, because a message is
    prose that gets reworded and an action attached to prose silently follows the wrong finding.
    An outcome with no safe action carries none, which is the required behaviour rather than a gap.
    """

    outcome_id: str
    severity: str
    remediation: str
    parameters: tuple[tuple[str, object], ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "outcome_id": self.outcome_id,
            "severity": self.severity,
            "remediation": self.remediation,
            "parameters": [[key, value] for key, value in self.parameters],
        }


def diagnostic_of(outcome: object) -> ProviderDiagnostic | None:
    """Project a typed outcome. `OK` is not a diagnostic and produces nothing to render."""

    if outcome is None:
        return None
    if not isinstance(outcome, PromptModelOutcome):
        _fail("diagnostic_outcome")
    if outcome.outcome_id is PromptModelOutcomeId.OK:
        return None
    return ProviderDiagnostic(
        outcome_id=outcome.outcome_id.value,
        severity=outcome.severity.value,
        remediation=outcome.remediation.value,
        parameters=tuple(outcome.parameters),
    )


@dataclass(frozen=True, slots=True)
class ProviderSettingsProjection:
    """Everything the Settings surface may know, and nothing a credential could travel in."""

    revision: int
    catalog_empty: bool
    profiles: tuple[ProviderProfileView, ...]
    selected_profile_id: str
    selected_model_id: str
    selected_model: ModelChoice | None
    readiness: ProviderReadiness
    disclosure: TransmissionDisclosure | None
    consent: ProviderConsentView | None
    consent_required: bool
    credential_required: bool
    credential_present: bool
    credential_last_four: str
    candidates: tuple[DiscoveryCandidate, ...]
    candidates_truncated: bool
    diagnostic: ProviderDiagnostic | None
    #: Whether anything has actually observed the selected provider. `UNREACHABLE` covers two
    #: different facts -- "it was asked and did not answer" and "nothing has asked it" -- and only
    #: the first of those entitles a surface to say the host is down. Presentation needs to tell
    #: them apart, so the projection carries the difference instead of letting a locale guess.
    reachability_observed: bool = False
    assisted_authoring: AssistedAuthoringState = field(
        default_factory=lambda: AssistedAuthoringState(False, False, False, False, False)
    )
    schema: str = PROVIDER_SETTINGS_SCHEMA

    def __post_init__(self) -> None:
        if self.credential_last_four != "":
            _fail("projection_credential_hint")
        selected_profile = next(
            (item for item in self.profiles if item.profile_id == self.selected_profile_id), None
        )
        if self.catalog_empty is not (not self.profiles):
            _fail("projection_catalog")
        if self.selected_profile_id and selected_profile is None:
            _fail("projection_selection")
        if self.selected_model_id:
            if (
                selected_profile is None
                or self.selected_model is None
                or self.selected_model_id != self.selected_model.model_id
                or self.selected_model.profile_id != self.selected_profile_id
            ):
                _fail("projection_selection")
        elif self.selected_model is not None:
            _fail("projection_selection")
        if self.consent is not None and self.consent.profile_id != self.selected_profile_id:
            _fail("projection_consent")
        exact_candidates = tuple(
            item for item in self.candidates if item.identifier == self.selected_model_id
        )
        if self.readiness is ProviderReadiness.READY and (
            not self.selected_model_id
            or len(exact_candidates) != 1
            or not exact_candidates[0].admitted
        ):
            _fail("projection_readiness")
        if self.assisted_authoring != replace(
            self.assisted_authoring,
            available=bool(self.profiles),
            selected=bool(self.selected_profile_id),
            ready=self.readiness is ProviderReadiness.READY,
            defaulted=False,
        ):
            _fail("projection_assisted_authoring")
        # Authorization is checked as an implication, not an equality. Ready and qualified are
        # necessary and the projection can see both, so a surface claiming authority without them
        # is refused here. They are not sufficient -- M22-13 also requires the exact weights to
        # have been observed this session -- and that proof is deliberately not projected, so the
        # projection must be able to accept a withheld authorization it cannot explain.
        if self.assisted_authoring.authorized_for_this_action and not (
            self.readiness is ProviderReadiness.READY
            and selected_profile is not None
            and selected_profile.qualification_state
            == PromptModelQualificationState.QUALIFIED.value
        ):
            _fail("projection_assisted_authoring")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "catalog_empty": self.catalog_empty,
            "profiles": [item.to_wire() for item in self.profiles],
            "selected_profile_id": self.selected_profile_id,
            "selected_model_id": self.selected_model_id,
            "selected_model": None
            if self.selected_model is None
            else self.selected_model.to_wire(),
            "readiness": self.readiness.value,
            "disclosure": None if self.disclosure is None else self.disclosure.to_wire(),
            "consent": None if self.consent is None else self.consent.to_wire(),
            "consent_required": self.consent_required,
            "credential_required": self.credential_required,
            "credential_present": self.credential_present,
            "credential_last_four": self.credential_last_four,
            "candidates": [item.to_wire() for item in self.candidates],
            "candidates_truncated": self.candidates_truncated,
            "diagnostic": None if self.diagnostic is None else self.diagnostic.to_wire(),
            "reachability_observed": self.reachability_observed,
            "assisted_authoring": self.assisted_authoring.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ReadinessObservation:
    """What one probe learned about one provider, at one moment.

    `reachable` is set only by something that actually asked. A refusal to transmit, a withheld
    consent or a missing credential leaves no observation at all, because none of those is
    evidence about the provider -- and a surface that reported them as "not answering" would be
    describing the user's machine on no evidence.
    """

    reachable: bool
    outcome: PromptModelOutcome | None = None
    candidates: tuple[DiscoveryCandidate, ...] = ()
    #: M22-13. Present only when a qualified profile was observed running the exact weights it was
    #: qualified against. Absent is the normal case, and absent never means "probably fine": a
    #: catalog-only profile, an unqualified family and a mismatched identity all produce no
    #: identity, so a caller that requires one cannot accidentally accept any of them. It is the
    #: raw observation, not authority: the session stamps its own counters onto it.
    identity: QualifiedIdentityObservation | None = None

    def __post_init__(self) -> None:
        if type(self.reachable) is not bool:
            _fail("observation_reachable")
        if self.outcome is not None and not isinstance(self.outcome, PromptModelOutcome):
            _fail("observation_outcome")
        if not isinstance(self.candidates, tuple):
            _fail("observation_candidates")
        if len(self.candidates) > MAX_DISCOVERY_ROWS:
            # SECURITY: an adapter census is untrusted provider data. Refuse an oversized census
            # before it can become session state, even though the projection also truncates it.
            _fail("observation_candidates")
        for candidate in self.candidates:
            if not isinstance(candidate, DiscoveryCandidate):
                _fail("observation_candidates")
        if self.identity is not None and not isinstance(
            self.identity, QualifiedIdentityObservation
        ):
            _fail("observation_identity")
        compatible_reachable_outcomes = {
            PromptModelOutcomeId.MODEL_MISSING,
            PromptModelOutcomeId.DIGEST_MISMATCH,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
            # A provider that answered but whose capabilities, context window or licence no longer
            # match the qualification is reachable and not ready. It gets its own outcome rather
            # than being folded into a digest mismatch, because what the operator has to do about
            # it is different.
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
        }
        if self.reachable:
            if (
                self.outcome is not None
                and self.outcome.outcome_id not in compatible_reachable_outcomes
            ):
                _fail("observation_contradiction")
        elif self.outcome is None or self.outcome.outcome_id is PromptModelOutcomeId.OK:
            _fail("observation_contradiction")
        if not self.reachable and self.candidates:
            _fail("observation_contradiction")
        if self.identity is not None and (not self.reachable or self.outcome is not None):
            # An identity observation is a statement that this provider is ready right now. It
            # cannot coexist with a silent host or with any outcome that says it is not.
            _fail("observation_contradiction")


@dataclass(frozen=True, slots=True)
class ProviderIntentResult:
    """What an intent did. A refusal names a closed reason and changes nothing."""

    accepted: bool
    projection: ProviderSettingsProjection
    rejection: ProviderIntentRejection | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROVIDER_SETTINGS_SCHEMA,
            "accepted": self.accepted,
            "rejection": None if self.rejection is None else self.rejection.value,
            "projection": self.projection.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ProviderExecutionSnapshot:
    """Private exact execution authority. Deliberately has no wire serializer."""

    profile: PromptModelProfile
    destination: AdmittedDestination
    selected_model_id: str
    model: ModelChoice
    provider_revision: int
    authority_epoch: int
    credential: RuntimeCredential | None
    consent: Callable[[], RemoteConsentRecord | None]

    def __post_init__(self) -> None:
        if not isinstance(self.profile, PromptModelProfile):
            _fail("execution_profile")
        if not isinstance(self.destination, AdmittedDestination):
            _fail("execution_destination")
        if (
            not isinstance(self.model, ModelChoice)
            or self.model.profile_id != self.profile.profile_id
            or self.selected_model_id != self.model.model_id
        ):
            _fail("execution_model")
        for value in (self.provider_revision, self.authority_epoch):
            if type(value) is not int or not 1 <= value <= 2_147_483_647:
                _fail("execution_revision")
        if self.credential is not None and not isinstance(self.credential, RuntimeCredential):
            _fail("execution_credential")
        if not callable(self.consent):
            _fail("execution_consent")


@dataclass(frozen=True, slots=True)
class ProviderExecutionDecision:
    outcome: PromptModelOutcome
    snapshot: ProviderExecutionSnapshot | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, PromptModelOutcome):
            _fail("execution_outcome")
        if self.snapshot is not None and not isinstance(self.snapshot, ProviderExecutionSnapshot):
            _fail("execution_snapshot")
        if (self.outcome.outcome_id is PromptModelOutcomeId.OK) is (self.snapshot is None):
            _fail("execution_decision")

    @property
    def admitted(self) -> bool:
        return self.snapshot is not None


@dataclass
class ProviderSettingsState:
    """The session's provider state. In memory, revisioned, and never written anywhere.

    The revision exists so the surface can tell a stale render from a current one. It moves on every
    accepted intent and on nothing else, so a refused intent cannot look like a change.
    """

    profiles: tuple[PromptModelProfile, ...] = ()
    _selected: str = ""
    _selected_model: str = ""
    _model_choice: ModelChoice | None = None
    _listing_sha256: str = ""
    _listed_at: float = 0.0
    _revision: int = 1
    _ledger: RemoteConsentLedger = field(default_factory=RemoteConsentLedger)
    _credentials: dict[str, RuntimeCredential] = field(default_factory=dict)
    _reachable: dict[str, bool] = field(default_factory=dict)
    _outcome: PromptModelOutcome | None = None
    _candidates: tuple[DiscoveryCandidate, ...] = ()
    _authority_epoch: int = 1
    _readiness_evidence: ProviderReadinessEvidence | None = None
    _today: Callable[[], date] = field(default=date.today, repr=False, compare=False)

    @classmethod
    def from_catalog(cls) -> ProviderSettingsState:
        """Build from the shipped catalog, which pins no profile at all by default."""

        return cls(profiles=load_prompt_model_catalog().profiles)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def readiness_evidence(self) -> ProviderReadinessEvidence | None:
        """The stamped proof, if the current selection has one. Never inferred, only observed."""

        return self._readiness_evidence

    @property
    def selected_profile_id(self) -> str:
        return self._selected

    @property
    def selected_model_id(self) -> str:
        return self._selected_model

    @property
    def model_choice(self) -> ModelChoice | None:
        return self._model_choice

    @property
    def candidates(self) -> tuple[DiscoveryCandidate, ...]:
        """Server-owned census, independent of the bounded presentation window."""
        return self._candidates

    @property
    def authority_epoch(self) -> int:
        return self._authority_epoch

    def _profile(self, profile_id: str) -> PromptModelProfile | None:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    def _bump(self) -> None:
        self._revision += 1
        self._authority_epoch += 1

    @staticmethod
    def _execution_refusal(
        outcome_id: PromptModelOutcomeId,
        remediation: PromptModelRemediation = PromptModelRemediation.NONE,
    ) -> ProviderExecutionDecision:
        return ProviderExecutionDecision(
            outcome=build_prompt_model_outcome(
                outcome_id,
                severity=ValidationSeverity.ERROR,
                remediation=remediation,
                parameters=(),
            ),
            snapshot=None,
        )

    def _readiness_proof_matches(self, profile: PromptModelProfile) -> bool:
        evidence = self._readiness_evidence
        return bool(
            evidence is not None
            and evidence.matches(
                profile,
                compute_endpoint_fingerprint(profile.endpoint),
                self._revision,
                self._authority_epoch,
                self._model_choice,
            )
        )

    def execution_decision(self) -> ProviderExecutionDecision:
        """Resolve exact private authority without opening an exchange or projecting a secret."""

        profile = self._profile(self._selected)
        if profile is None:
            return self._execution_refusal(
                PromptModelOutcomeId.MODEL_MISSING, PromptModelRemediation.SELECT_MODEL
            )
        # SECURITY: technical readiness is not roadmap/provider qualification.
        if profile.qualification_state is not PromptModelQualificationState.QUALIFIED:
            return self._execution_refusal(PromptModelOutcomeId.PROFILE_NOT_QUALIFIED)
        model = self._model_choice
        if model is None or not self._choice_matches_census():
            return self._execution_refusal(
                PromptModelOutcomeId.MODEL_MISSING, PromptModelRemediation.SELECT_MODEL
            )
        if self.readiness() is not ProviderReadiness.READY:
            return self._execution_refusal(
                PromptModelOutcomeId.CAPABILITY_MISMATCH,
                PromptModelRemediation.SELECT_MODEL,
            )
        # M22-13. This function, not the projection, is what admits a real execution: the product
        # route reaches `begin_assisted_execution` and never reads `authorized_for_this_action`.
        # So the requirement that the exact qualified identity was observed this session has to be
        # enforced here too, or the surface would refuse an action the backend would still perform.
        if not self._readiness_proof_matches(profile):
            return self._execution_refusal(
                PromptModelOutcomeId.CAPABILITY_MISMATCH,
                PromptModelRemediation.SELECT_MODEL,
            )
        try:
            destination = admit_egress_destination(profile.family, profile.endpoint)
        except PromptModelEgressError:
            return self._execution_refusal(
                PromptModelOutcomeId.EGRESS_REFUSED,
                PromptModelRemediation.CORRECT_ENDPOINT,
            )
        credential = self._credentials.get(self._selected)
        route = route_for_family(profile.family)
        if route.consent_required:
            remote = admit_remote_transmission(
                profile=profile,
                selected_profile_id=self._selected,
                consent=self._ledger.record_for(self._selected),
                credential=credential,
                carries_media=False,
            )
            if not remote.admitted:
                return ProviderExecutionDecision(outcome=remote.outcome, snapshot=None)
        selected = self._selected
        return ProviderExecutionDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.OK,
                severity=ValidationSeverity.INFO,
                remediation=PromptModelRemediation.NONE,
                parameters=(("profile_id", profile.profile_id),),
            ),
            snapshot=ProviderExecutionSnapshot(
                profile=profile,
                destination=destination,
                selected_model_id=self._selected_model,
                model=model,
                provider_revision=self._revision,
                authority_epoch=self._authority_epoch,
                credential=credential,
                consent=lambda: self._ledger.record_for(selected),
            ),
        )

    def observe_reachable(self, profile_id: str, reachable: bool) -> None:
        """Record what an adapter saw. This module never opens a socket to find out."""

        if type(reachable) is not bool:
            _fail("reachability_flag")
        self._reachable[profile_id] = reachable

    def record_outcome(self, outcome: PromptModelOutcome | None) -> None:
        self._outcome = outcome

    def record_candidates(self, candidates: Sequence[DiscoveryCandidate]) -> None:
        entries = tuple(candidates)
        if len(entries) > MAX_DISCOVERY_ROWS:
            _fail("candidates")
        for entry in entries:
            if not isinstance(entry, DiscoveryCandidate):
                _fail("candidates")
        self._candidates = entries
        material = json.dumps(
            [entry.to_wire() for entry in entries], sort_keys=True, separators=(",", ":")
        )
        self._listing_sha256 = "sha256:" + sha256(material.encode("utf-8")).hexdigest()
        self._listed_at = monotonic()
        if self._selected_model:
            exact = [entry for entry in entries if entry.identifier == self._selected_model]
            if len(exact) == 1 and exact[0].admitted:
                self._model_choice = ModelChoice(
                    self._selected,
                    self._selected_model,
                    self._listing_sha256,
                    self._listed_at,
                    exact[0].metadata,
                )
            else:
                if self._outcome is None:
                    self._outcome = build_prompt_model_outcome(
                        PromptModelOutcomeId.MODEL_MISSING,
                        severity=ValidationSeverity.ERROR,
                        remediation=PromptModelRemediation.SELECT_MODEL,
                        parameters=(),
                    )
                self._selected_model = ""
                self._model_choice = None
                self._readiness_evidence = None

    def _choice_matches_census(self) -> bool:
        choice = self._model_choice
        exact = [entry for entry in self._candidates if entry.identifier == self._selected_model]
        return bool(
            choice is not None
            and choice.profile_id == self._selected
            and choice.model_id == self._selected_model
            and choice.listing_sha256 == self._listing_sha256
            and choice.listed_at_monotonic == self._listed_at
            and len(exact) == 1
            and exact[0].admitted
        )

    def _invalidate_selected_observation(self, *, clear_candidates: bool = True) -> None:
        """Discard readiness evidence collected under settings that are about to change."""

        self._reachable.pop(self._selected, None)
        self._outcome = None
        # M22-13. Readiness evidence is the most perishable thing here: it names an exact set of
        # weights observed at one moment. Anything that invalidates the observation invalidates it.
        self._readiness_evidence = None
        if clear_candidates:
            self._candidates = ()
            self._listing_sha256 = ""
            self._listed_at = 0.0
            self._model_choice = None
            self._selected_model = ""

    def _revoke_consent_if_recorded(self, profile_id: str) -> None:
        """Invalidate remote authority without creating a consent decision the user never made."""

        if profile_id and self._ledger.record_for(profile_id) is not None:
            # CRITICAL: changing provider/credential must not let an older consent record silently
            # authorize the new selection or become active again when the user switches back.
            self._ledger.revoke(profile_id)

    def recheck(self, probe: object = None) -> ProviderIntentResult:
        """Re-evaluate readiness, observing the provider when a probe is supplied.

        Reached only from an explicit `RECHECK_READINESS` intent. Nothing here runs because a page
        was opened or on a timer: an observation costs a request, and for the remote family that
        request leaves the machine.

        The consent gate runs *here* rather than in the adapter that owns the socket. If the
        adapter checked it, "no remote probe without consent and a credential" would be a
        convention the next caller could forget; run from the core, it is a property of the only
        code path that can reach a probe at all.
        """

        profile = self._profile(self._selected)
        if profile is None:
            # A recheck with nothing selected still answers: it reports that nothing is
            # configured, which is a fact rather than a refusal.
            self.record_outcome(None)
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())
        if probe is None:
            # IMPORTANT: an explicit recheck without an executable probe is not fresh evidence.
            # Clear the old answer so a previous READY result cannot survive a no-probe call.
            self._invalidate_selected_observation()
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())
        if not callable(probe):
            _fail("readiness_probe")
        credential = self._credentials.get(self._selected)
        if route_for_family(profile.family).consent_required:
            consent = self._ledger.record_for(self._selected)
            decision = admit_remote_transmission(
                profile=profile,
                selected_profile_id=self._selected,
                consent=consent,
                credential=credential,
                carries_media=False,
            )
            if not decision.admitted:
                self.record_outcome(decision.outcome)
                self._bump()
                return ProviderIntentResult(accepted=True, projection=self.project())
        try:
            observation = probe(profile, credential)
        except Exception:
            # IMPORTANT: a failed reload cannot retain the old census, choice or READY proof.
            # Preserve only a closed outcome; exception prose may contain provider secrets.
            self._invalidate_selected_observation()
            self.record_outcome(
                build_prompt_model_outcome(
                    PromptModelOutcomeId.PROVIDER_ERROR,
                    severity=ValidationSeverity.ERROR,
                    remediation=PromptModelRemediation.RETRY_LATER,
                    parameters=(),
                )
            )
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())
        if not isinstance(observation, ReadinessObservation):
            self._invalidate_selected_observation()
            self.record_outcome(
                build_prompt_model_outcome(
                    PromptModelOutcomeId.MALFORMED_RESPONSE,
                    severity=ValidationSeverity.ERROR,
                    remediation=PromptModelRemediation.RETRY_LATER,
                    parameters=(),
                )
            )
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())
        self.observe_reachable(self._selected, observation.reachable)
        self.record_outcome(observation.outcome)
        # IMPORTANT: an empty census is still an observation and must replace stale candidates.
        self.record_candidates(observation.candidates)
        self._bump()
        # The probe reports what answered; only this object knows under which authority it asked,
        # so the stamp happens here and nowhere else -- and after the bump, because the recheck is
        # itself the accepted intent this evidence belongs to. Stamping the pre-bump revision would
        # produce evidence that never matches the state that just created it. A probe that saw
        # nothing qualified clears any previous evidence rather than leaving it to age quietly.
        self._readiness_evidence = (
            None
            if observation.identity is None or observation.identity.profile_id != self._selected
            else stamp_readiness_evidence(
                observation.identity,
                provider_revision=self._revision,
                authority_epoch=self._authority_epoch,
            )
        )
        return ProviderIntentResult(accepted=True, projection=self.project())

    def readiness(self) -> ProviderReadiness:
        """Decide readiness here, so no presentation layer can decide it differently."""

        profile = self._profile(self._selected)
        if profile is None:
            return ProviderReadiness.NOT_CONFIGURED
        if not self._choice_matches_census():
            return ProviderReadiness.NOT_CONFIGURED
        route = route_for_family(profile.family)
        if profile.capabilities.requires_credential and self._selected not in self._credentials:
            return ProviderReadiness.NOT_CONFIGURED
        if route.consent_required:
            record = self._ledger.record_for(self._selected)
            if record is None or not record.granted or not record.network_permitted:
                return ProviderReadiness.NOT_CONFIGURED
        reachable = self._reachable.get(self._selected)
        if reachable is None:
            return ProviderReadiness.UNREACHABLE
        if not reachable:
            return ProviderReadiness.UNREACHABLE
        exact_candidates = tuple(
            candidate
            for candidate in self._candidates
            if candidate.identifier == self._selected_model
        )
        # SECURITY: readiness is current evidence, not a memory of a prior good census. One exact
        # admitted row is required; duplicate IDs and a later empty census both fail closed.
        if len(exact_candidates) != 1 or not exact_candidates[0].admitted:
            return ProviderReadiness.INCOMPATIBLE
        if self._outcome is not None and self._outcome.outcome_id in {
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
            PromptModelOutcomeId.DIGEST_MISMATCH,
            PromptModelOutcomeId.MODEL_MISSING,
            PromptModelOutcomeId.MALFORMED_RESPONSE,
        }:
            return ProviderReadiness.INCOMPATIBLE
        return ProviderReadiness.READY

    def project(self) -> ProviderSettingsProjection:
        profile = self._profile(self._selected)
        route = None if profile is None else route_for_family(profile.family)
        record = self._ledger.record_for(self._selected) if self._selected else None
        credential = self._credentials.get(self._selected)
        candidates = self._candidates[:MAX_PROJECTED_CANDIDATES]
        readiness = self.readiness()
        return ProviderSettingsProjection(
            revision=self._revision,
            catalog_empty=not self.profiles,
            profiles=tuple(view_of_profile(item) for item in self.profiles),
            selected_profile_id=self._selected,
            selected_model_id=self._selected_model,
            selected_model=self._model_choice,
            readiness=readiness,
            disclosure=(
                None
                if profile is None or route is None
                else describe_transmission(route, profile.capabilities, profile=profile)
            ),
            consent=(
                None
                if record is None
                else ProviderConsentView(
                    profile_id=record.profile_id,
                    status=record.status.value,
                    network_permitted=record.network_permitted,
                    media_upload_consented=record.media_upload_consented,
                    revision=record.revision,
                )
            ),
            consent_required=bool(route is not None and route.consent_required),
            credential_required=bool(
                profile is not None and profile.capabilities.requires_credential
            ),
            credential_present=credential is not None,
            # CRITICAL: credential presence is sufficient UI truth. Returning even a suffix turns
            # secret material into response data and lets browser sessions correlate credentials.
            credential_last_four="",
            candidates=candidates,
            candidates_truncated=len(self._candidates) > MAX_PROJECTED_CANDIDATES,
            diagnostic=diagnostic_of(self._outcome),
            reachability_observed=self._selected in self._reachable,
            assisted_authoring=AssistedAuthoringState(
                available=bool(self.profiles),
                selected=profile is not None,
                ready=readiness is ProviderReadiness.READY,
                authorized_for_this_action=bool(
                    profile is not None
                    and profile.qualification_state is PromptModelQualificationState.QUALIFIED
                    and readiness is ProviderReadiness.READY
                    # M22-13. A qualified row plus a reachable host is not authority: the exact
                    # qualified identity has to have been observed this session under these
                    # counters. Every other fact here can be true of a provider that swapped it.
                    and self._readiness_proof_matches(profile)
                ),
                defaulted=False,
            ),
        )

    def _refuse(self, rejection: ProviderIntentRejection) -> ProviderIntentResult:
        return ProviderIntentResult(accepted=False, projection=self.project(), rejection=rejection)

    def preflight_rejection(
        self, intent: object, values: Mapping[str, object]
    ) -> ProviderIntentRejection | None:
        """Reject invalid connection mutations before registry cancellation/reservation."""
        profile = self._profile(self._selected)
        connection = profile is not None and not isinstance(profile, LegacyPromptModelProfile)
        if connection and intent in {
            ProviderSettingsIntent.SUBMIT_CREDENTIAL,
            ProviderSettingsIntent.GRANT_CONSENT,
        }:
            return ProviderIntentRejection.UNKNOWN_INTENT
        if connection and intent in {
            ProviderSettingsIntent.SELECT_MODEL,
            ProviderSettingsIntent.CONNECT_AND_REFRESH,
            ProviderSettingsIntent.RECHECK_READINESS,
        }:
            if values.get("_legacy_request") is True:
                return ProviderIntentRejection.UNKNOWN_INTENT
            if (
                values.get("profile_id") != self._selected
                or type(values.get("expected_revision")) is not int
                or values["expected_revision"] != self._revision
            ):
                return ProviderIntentRejection.STALE_REVISION
        if intent is ProviderSettingsIntent.SELECT_MODEL and profile is not None:
            model_id = values.get("model_id")
            exact = [
                candidate for candidate in self._candidates if candidate.identifier == model_id
            ]
            if (
                not isinstance(model_id, str)
                or not self._listing_sha256
                or len(exact) != 1
                or not exact[0].admitted
                or (isinstance(profile, LegacyPromptModelProfile) and model_id != profile.model_id)
            ):
                return ProviderIntentRejection.UNKNOWN_MODEL
            try:
                ModelChoice(
                    self._selected,
                    model_id,
                    self._listing_sha256,
                    self._listed_at,
                    exact[0].metadata,
                )
            except PromptModelContractError:
                return ProviderIntentRejection.UNKNOWN_MODEL
        if intent is ProviderSettingsIntent.CONNECT_AND_REFRESH and profile is not None:
            if not route_for_family(profile.family).consent_required:
                if set(values) != {"profile_id", "expected_revision"}:
                    return ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE
            else:
                replacement = "credential" in values
                if replacement:
                    try:
                        RuntimeCredential(values["credential"])  # type: ignore[arg-type]
                    except PromptModelContractError:
                        return ProviderIntentRejection.CREDENTIAL_REJECTED
                grant = (
                    values.get("network_permitted") is True
                    and values.get("media_upload_consented") is False
                )
                if (
                    bool({"network_permitted", "media_upload_consented"} & set(values))
                    and not grant
                ):
                    return ProviderIntentRejection.CONSENT_REQUIRED
                if not replacement and self._selected not in self._credentials:
                    return ProviderIntentRejection.CREDENTIAL_REJECTED
                consent = self._ledger.record_for(self._selected)
                if not grant and (
                    replacement
                    or consent is None
                    or not consent.granted
                    or not consent.network_permitted
                ):
                    return ProviderIntentRejection.CONSENT_REQUIRED
        return None

    def apply(
        self, intent: object, payload: object = None, *, readiness_probe: object = None
    ) -> ProviderIntentResult:
        """Execute one intent. Every control the surface renders arrives here or does nothing."""

        if not isinstance(intent, ProviderSettingsIntent):
            return self._refuse(ProviderIntentRejection.UNKNOWN_INTENT)
        values: Mapping[str, object] = payload if isinstance(payload, Mapping) else {}
        rejection = self.preflight_rejection(intent, values)
        if rejection is not None:
            return self._refuse(rejection)

        if intent is ProviderSettingsIntent.READ_PROJECTION:
            # Reading current facts is intentionally not an observation and does not move revision.
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.SELECT_PROFILE:
            if not self.profiles:
                return self._refuse(ProviderIntentRejection.CATALOG_EMPTY)
            requested = values.get("profile_id")
            if not isinstance(requested, str) or self._profile(requested) is None:
                # A profile the catalog does not contain is not selectable from here. The scan
                # detail may name an unpinned candidate; naming it is not an offer to admit it.
                return self._refuse(ProviderIntentRejection.UNKNOWN_PROFILE)
            previous = self._selected
            self._invalidate_selected_observation()
            self._revoke_consent_if_recorded(previous)
            if requested != previous:
                self._revoke_consent_if_recorded(requested)
            self._selected = requested
            self._selected_model = ""
            self._reachable.pop(requested, None)
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.CLEAR_SELECTION:
            if not self._selected:
                return self._refuse(ProviderIntentRejection.NO_SELECTION)
            self._invalidate_selected_observation()
            self._revoke_consent_if_recorded(self._selected)
            self._selected = ""
            self._selected_model = ""
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.CONNECT_AND_REFRESH:
            profile = self._profile(self._selected)
            if profile is None:
                return self._refuse(ProviderIntentRejection.NO_SELECTION)
            if (
                values.get("profile_id") != self._selected
                or type(values.get("expected_revision")) is not int
                or values["expected_revision"] != self._revision
            ):
                return self._refuse(ProviderIntentRejection.STALE_REVISION)
            remote = route_for_family(profile.family).consent_required
            replacement = None
            if not remote:
                if set(values) != {"profile_id", "expected_revision"}:
                    return self._refuse(ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE)
            else:
                if "credential" in values:
                    try:
                        replacement = RuntimeCredential(values["credential"])  # type: ignore[arg-type]
                    except PromptModelContractError:
                        return self._refuse(ProviderIntentRejection.CREDENTIAL_REJECTED)
                grant = (
                    values.get("network_permitted") is True
                    and values.get("media_upload_consented") is False
                )
                flags_present = bool({"network_permitted", "media_upload_consented"} & set(values))
                if flags_present and not grant:
                    return self._refuse(ProviderIntentRejection.CONSENT_REQUIRED)
                consent = self._ledger.record_for(self._selected)
                held = replacement or self._credentials.get(self._selected)
                if held is None:
                    return self._refuse(ProviderIntentRejection.CREDENTIAL_REJECTED)
                if replacement is not None and not grant:
                    return self._refuse(ProviderIntentRejection.CONSENT_REQUIRED)
                if not grant and (
                    consent is None or not consent.granted or not consent.network_permitted
                ):
                    return self._refuse(ProviderIntentRejection.CONSENT_REQUIRED)
                # SECURITY: all ownership, credential and explicit-scope checks precede mutation.
                # A failed list may retain a valid key/grant, but never old model/readiness proof.
                if replacement is not None:
                    self._revoke_consent_if_recorded(self._selected)
                    self._credentials[self._selected] = replacement
                if grant:
                    self._ledger.grant(
                        self._selected, network_permitted=True, media_upload_consented=False
                    )
            self._invalidate_selected_observation(clear_candidates=replacement is not None)
            self._bump()
            return self.recheck(readiness_probe)

        if intent is ProviderSettingsIntent.SELECT_MODEL:
            profile = self._profile(self._selected)
            if profile is None:
                return self._refuse(ProviderIntentRejection.NO_SELECTION)
            requested_model = values.get("model_id")
            if not isinstance(profile, LegacyPromptModelProfile) and (
                values.get("profile_id") != self._selected
                or type(values.get("expected_revision")) is not int
                or values["expected_revision"] != self._revision
            ):
                return self._refuse(ProviderIntentRejection.STALE_REVISION)
            exact_candidates = tuple(
                candidate
                for candidate in self._candidates
                if candidate.identifier == requested_model
            )
            # SECURITY: ids are lookup inputs, not authority. Only this connection's current,
            # unique admitted census row can create the model choice; stale lists never execute.
            if (
                not isinstance(requested_model, str)
                or (
                    isinstance(profile, LegacyPromptModelProfile)
                    and requested_model != profile.model_id
                )
                or len(exact_candidates) != 1
                or not exact_candidates[0].admitted
                or not self._listing_sha256
            ):
                return self._refuse(ProviderIntentRejection.UNKNOWN_MODEL)
            try:
                choice = ModelChoice(
                    self._selected,
                    requested_model,
                    self._listing_sha256,
                    self._listed_at,
                    exact_candidates[0].metadata,
                )
            except PromptModelContractError:
                return self._refuse(ProviderIntentRejection.UNKNOWN_MODEL)
            self._selected_model = requested_model
            self._model_choice = choice
            self._invalidate_selected_observation(clear_candidates=False)
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.CLEAR_MODEL:
            if not self._selected:
                return self._refuse(ProviderIntentRejection.NO_SELECTION)
            if not self._selected_model:
                return self._refuse(ProviderIntentRejection.NO_MODEL_SELECTION)
            self._selected_model = ""
            self._model_choice = None
            self._invalidate_selected_observation(clear_candidates=False)
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.RECHECK_READINESS:
            # An explicit user action, never a background poll, and it is answerable with nothing
            # selected: "check again" then reports that nothing is configured, which is a fact
            # rather than a refusal. It clears the last typed failure so a stale one cannot outlive
            # the condition that produced it. `recheck` is the single implementation; a caller with
            # a probe reaches it through this one call.
            return self.recheck(readiness_probe)

        profile = self._profile(self._selected)
        if profile is None:
            return self._refuse(ProviderIntentRejection.NO_SELECTION)
        route = route_for_family(profile.family)

        if intent is ProviderSettingsIntent.SUBMIT_CREDENTIAL:
            if not profile.capabilities.requires_credential:
                return self._refuse(ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE)
            secret = values.get("credential")
            try:
                held = RuntimeCredential(secret)  # type: ignore[arg-type]
            except PromptModelContractError:
                # The refusal says a credential was rejected and nothing about its content.
                return self._refuse(ProviderIntentRejection.CREDENTIAL_REJECTED)
            self._credentials[self._selected] = held
            self._revoke_consent_if_recorded(self._selected)
            self._invalidate_selected_observation()
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.DISCARD_CREDENTIAL:
            if self._credentials.pop(self._selected, None) is None:
                return self._refuse(ProviderIntentRejection.CREDENTIAL_NOT_APPLICABLE)
            self._revoke_consent_if_recorded(self._selected)
            self._invalidate_selected_observation()
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.GRANT_CONSENT:
            if not route.consent_required:
                # Recording consent for a family that needs none would put a decision in the ledger
                # the user was never asked to make.
                return self._refuse(ProviderIntentRejection.CONSENT_NOT_APPLICABLE)
            supports_media = any(
                media.value != "text" for media in profile.capabilities.accepted_media
            )
            self._ledger.grant(
                self._selected,
                network_permitted=values.get("network_permitted") is True,
                # SECURITY: hidden client flags cannot manufacture upload authority for a
                # text-only profile. Future media-capable profiles remain explicitly gated.
                media_upload_consented=(
                    supports_media and values.get("media_upload_consented") is True
                ),
            )
            self._invalidate_selected_observation(clear_candidates=False)
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        if intent is ProviderSettingsIntent.REVOKE_CONSENT:
            if not route.consent_required:
                return self._refuse(ProviderIntentRejection.CONSENT_NOT_APPLICABLE)
            if self._ledger.record_for(self._selected) is None:
                return self._refuse(ProviderIntentRejection.CONSENT_NOT_APPLICABLE)
            self._ledger.revoke(self._selected)
            # SECURITY: a census obtained with a withdrawn grant cannot create another choice.
            self._invalidate_selected_observation()
            self._bump()
            return ProviderIntentResult(accepted=True, projection=self.project())

        return self._refuse(ProviderIntentRejection.UNKNOWN_INTENT)


def remote_families() -> tuple[PromptModelFamily, ...]:
    """The families whose route requires consent. One entry today; derived, not hard-coded."""

    return tuple(
        family
        for family, route in sorted(
            ((family, route_for_family(family)) for family in PromptModelFamily),
            key=lambda pair: pair[0].value,
        )
        if route.consent_required
    )


def consent_status_of(record: object) -> ProviderConsentStatus | None:
    """The recorded status, or `None` when no decision has been made in this session."""

    if record is None:
        return None
    if not isinstance(record, RemoteConsentRecord):
        _fail("consent_record")
    return record.status


__all__ = [
    "CONSENT_SCOPE",
    "MAX_PROJECTED_CANDIDATES",
    "PROVIDER_SETTINGS_SCHEMA",
    "REMOTE_FAMILY",
    "ProviderConsentView",
    "ProviderDiagnostic",
    "ProviderIntentRejection",
    "ProviderIntentResult",
    "ProviderExecutionDecision",
    "ProviderExecutionSnapshot",
    "ProviderProfileView",
    "ProviderReadiness",
    "ProviderSettingsIntent",
    "ProviderSettingsProjection",
    "ProviderSettingsState",
    "ReadinessObservation",
    "TransmissionDisclosure",
    "consent_status_of",
    "describe_transmission",
    "diagnostic_of",
    "remote_families",
    "view_of_profile",
]
