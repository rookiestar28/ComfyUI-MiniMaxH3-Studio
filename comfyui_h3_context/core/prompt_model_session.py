"""M22-03 prompt-model session contract: what a local transport must do, judged in pure code.

The transports themselves live in `adapters/prompt_model_transport.py`, because they open sockets
and start subprocesses. Everything that decides *whether* a request may be issued, *what* the
request looks like, and *how* an answer or a failure is classified lives here, where it can be
tested without a server.

Three decisions are worth stating plainly.

**Identity is verified at action time.** A local daemon is a mutable external resource: the
model behind a selected name can be swapped between the moment the user chooses it and the moment
the action starts. The action resolves one live identity and shares it with its bounded repair;
standalone sessions each resolve a new identity. The observed identity is checked against the
pinned catalog entry. Where the runtime reports a digest the check is exact; where it reports none,
the check falls back to an exact identifier match and the receipt says `digest_unverified` rather
than claiming a verification that did not happen.

**Discovery is diagnostic; admission stays closed.** The catalog is the only thing that admits a
model. Discovery exists to explain why something the user can see is not usable, one closed reason
at a time, and it never resolves an ambiguity by guessing or reads a capability out of a filename.

**An unload never races a live request.** The arbiter defers it and says so, rather than tearing a
model out from under a generation that is still running.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import NoReturn, Protocol

from .contracts import ValidationSeverity
from .prompt_model_budget import PromptModelBudgetPlan
from .prompt_model_dialects import (
    DialectAnswer,
    RequestOptions,
    anthropic_messages,
    ollama_chat,
    openai_chat,
)
from .prompt_model_dialects.common import draft_schema
from .prompt_model_execution_budget import options_for_metadata
from .prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    AdmittedDestination,
    LegacyPromptModelProfile,
    ModelChoice,
    ModelMetadata,
    PromptModelCapabilities,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelMediaKind,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelRemediation,
    build_prompt_model_outcome,
)

PROMPT_MODEL_SESSION_SCHEMA = "h3-context-prompt-model-session/1"

MAX_MESSAGE_CHARACTERS = 200_000
MAX_MESSAGES = 64
MAX_IMAGE_PAYLOADS = 8
MAX_IMAGE_PAYLOAD_CHARACTERS = 8_000_000
MAX_IDENTIFIER_CHARACTERS = 128

# The two local families this item delivers. `in_process_gguf` is declared by M22-01 and is
# deliberately absent: an in-process native runtime raises the item's risk class, so it gets the
# out-of-process probe contract here and a transport only from an item that accepts that risk.
LOCAL_TRANSPORT_FAMILIES: frozenset[PromptModelFamily] = frozenset(
    {PromptModelFamily.LOOPBACK_SERVER, PromptModelFamily.OLLAMA}
)

# Every family that has a transport at all. The remote one joined in M22-04 and reaches the network
# only through the separate consent gate in `core/remote_prompt_model.py`; being here means a
# request can be *shaped* for it, never that one may be sent.
TRANSPORT_FAMILIES: frozenset[PromptModelFamily] = LOCAL_TRANSPORT_FAMILIES | frozenset(
    {PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, PromptModelFamily.REMOTE_ANTHROPIC}
)

# The two families speaking the OpenAI chat-completions shape. Sharing the renderer keeps one
# request shape per wire dialect rather than one per family.
OPENAI_COMPATIBLE_FAMILIES: frozenset[PromptModelFamily] = frozenset(
    {PromptModelFamily.LOOPBACK_SERVER, PromptModelFamily.REMOTE_OPENAI_COMPATIBLE}
)


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _text(value: object, maximum: int, code: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(code)
    if not allow_empty and not value:
        _fail(code)
    if len(value) > maximum:
        _fail(code)
    return value


class PromptModelRole(str, Enum):
    """Closed message roles. A transport maps these; it never invents one."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class IdentityVerification(str, Enum):
    """How firmly the live model was tied to the pinned catalog entry."""

    DIGEST_MATCHED = "digest_matched"
    DIGEST_UNVERIFIED = "digest_unverified"


class DiscoveryRejection(str, Enum):
    """Closed reasons a visible candidate is not admitted. Ambiguity is reported, never resolved."""

    ADMITTED = "admitted"
    CLOUD_ROUTED = "cloud_routed"
    MISSING_ROOT = "missing_root"
    NO_CANDIDATE = "no_candidate"
    UNPAIRED_PROJECTOR = "unpaired_projector"
    AMBIGUOUS_FOLDER = "ambiguous_folder"
    UNPINNED = "unpinned"


class UnloadDisposition(str, Enum):
    """What happened to an unload request."""

    PERFORMED = "performed"
    DEFERRED_IN_FLIGHT = "deferred_in_flight"
    NOT_LOADED = "not_loaded"


class NativeProbeStatus(str, Enum):
    """Outcome of the out-of-process capability probe. Every failure blocks the family."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class PromptModelMessage:
    """One turn of the request. Text only; images travel beside the messages, bounded separately."""

    role: PromptModelRole
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, PromptModelRole):
            _fail("message_role")
        _text(self.text, MAX_MESSAGE_CHARACTERS, "message_text")


@dataclass(frozen=True, slots=True)
class PromptModelSessionRequest:
    """Everything needed to issue one request, and nothing that could issue an unadmitted one.

    The destination and the budget plan are both guarded types, so a caller cannot assemble this
    from a raw URL or an unplanned workload.
    """

    profile: PromptModelProfile
    destination: AdmittedDestination
    plan: PromptModelBudgetPlan
    messages: tuple[PromptModelMessage, ...]
    image_payloads: tuple[str, ...] = ()
    model: ModelChoice | None = None
    base_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.base_output_tokens is not None and (
            type(self.base_output_tokens) is not int
            or not 1 <= self.base_output_tokens <= 4_194_304
            or self.model is None
        ):
            _fail("request_base_output")
        if not isinstance(self.profile, PromptModelProfile):
            _fail("request_profile")
        if self.model is not None:
            if (
                not isinstance(self.model, ModelChoice)
                or self.model.profile_id != self.profile.profile_id
            ):
                _fail("request_model")
        elif not isinstance(self.profile, LegacyPromptModelProfile):
            _fail("request_model")
        if not isinstance(self.destination, AdmittedDestination):
            _fail("request_destination")
        if not isinstance(self.plan, PromptModelBudgetPlan):
            _fail("request_plan")
        if self.destination.family is not self.profile.family:
            _fail("request_family_mismatch")
        if self.plan.family is not self.profile.family:
            _fail("request_family_mismatch")
        if not isinstance(self.messages, tuple) or not self.messages:
            _fail("request_messages")
        if len(self.messages) > MAX_MESSAGES:
            _fail("request_messages")
        for message in self.messages:
            if not isinstance(message, PromptModelMessage):
                _fail("request_messages")
        if not isinstance(self.image_payloads, tuple):
            _fail("request_images")
        if len(self.image_payloads) > MAX_IMAGE_PAYLOADS:
            _fail("request_images")
        for payload in self.image_payloads:
            _text(payload, MAX_IMAGE_PAYLOAD_CHARACTERS, "request_images")

    @property
    def family(self) -> PromptModelFamily:
        return self.profile.family

    @property
    def capabilities(self) -> PromptModelCapabilities:
        return self.profile.capabilities

    @property
    def model_id(self) -> str:
        if self.model is not None:
            return self.model.model_id
        if isinstance(self.profile, LegacyPromptModelProfile):
            return self.profile.model_id
        _fail("request_model")


@dataclass(frozen=True, slots=True)
class LiveModelIdentity:
    """What the runtime says it is holding right now, as observed by an adapter."""

    model_id: str
    digest: str | None
    metadata: ModelMetadata | None = None

    def __post_init__(self) -> None:
        _text(self.model_id, MAX_IDENTIFIER_CHARACTERS, "identity_model_id")
        if self.digest is not None:
            _text(self.digest, MAX_IDENTIFIER_CHARACTERS, "identity_digest")
        if self.metadata is not None and not isinstance(self.metadata, ModelMetadata):
            _fail("identity_metadata")


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    """The result of re-checking the live model against the pinned entry."""

    outcome: PromptModelOutcome
    verification: IdentityVerification | None

    @property
    def admitted(self) -> bool:
        return self.verification is not None


def verify_live_identity(
    profile: object, observed: object, model: ModelChoice | None = None
) -> IdentityDecision:
    """Re-tie a live model to its pinned entry, or refuse. Never substitutes a near match."""

    if not isinstance(profile, PromptModelProfile):
        _fail("identity_profile")
    if not isinstance(observed, LiveModelIdentity):
        _fail("identity_observation")

    if isinstance(profile, LegacyPromptModelProfile):
        expected_model = profile.model_id
        expected_digest = profile.model_digest
    elif isinstance(model, ModelChoice) and model.profile_id == profile.profile_id:
        expected_model = model.model_id
        expected_digest = None if model.metadata is None else model.metadata.model_digest
    else:
        _fail("identity_model")
    if observed.model_id != expected_model:
        return IdentityDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.MODEL_MISSING,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(("expected_model_id", expected_model),),
            ),
            verification=None,
        )
    if observed.digest is None or expected_digest is None:
        # The runtime publishes no digest. Reporting a match anyway would claim a check that never
        # ran, so the identifier match is admitted and the weaker basis travels with the receipt.
        return IdentityDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.OK,
                severity=ValidationSeverity.INFO,
                remediation=PromptModelRemediation.NONE,
                parameters=(("verification", IdentityVerification.DIGEST_UNVERIFIED.value),),
            ),
            verification=IdentityVerification.DIGEST_UNVERIFIED,
        )
    if observed.digest != expected_digest:
        return IdentityDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.DIGEST_MISMATCH,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(("model_id", expected_model),),
            ),
            verification=None,
        )
    return IdentityDecision(
        outcome=build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(("verification", IdentityVerification.DIGEST_MATCHED.value),),
        ),
        verification=IdentityVerification.DIGEST_MATCHED,
    )


def admit_session_request(request: object) -> PromptModelOutcome:
    """Last gate before a socket is opened: family, media and byte ceilings, all fail-closed."""

    if not isinstance(request, PromptModelSessionRequest):
        _fail("session_request")
    if request.family not in TRANSPORT_FAMILIES:
        return build_prompt_model_outcome(
            PromptModelOutcomeId.CAPABILITY_MISMATCH,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.SELECT_MODEL,
            parameters=(("family", request.family.value),),
        )
    accepted = request.capabilities.accepted_media
    # M22-13. The local Ollama lane is text-only by family, not by catalog data. The curated model
    # itself reports a `vision` capability, so a row that copied its capabilities off `/api/show`
    # instead of the curated policy would otherwise open a media path this lane never qualified.
    if request.family is PromptModelFamily.OLLAMA:
        accepted = frozenset(kind for kind in accepted if kind is PromptModelMediaKind.TEXT)
    if request.image_payloads and PromptModelMediaKind.IMAGE not in accepted:
        return build_prompt_model_outcome(
            PromptModelOutcomeId.UNSUPPORTED_MEDIA,
            severity=ValidationSeverity.ERROR,
            remediation=PromptModelRemediation.CHANGE_MEDIA,
            parameters=(("media", PromptModelMediaKind.IMAGE.value),),
        )
    return build_prompt_model_outcome(
        PromptModelOutcomeId.OK,
        severity=ValidationSeverity.INFO,
        remediation=PromptModelRemediation.NONE,
        parameters=(("family", request.family.value),),
    )


#: The wire name of the draft contract the assisted-draft adapter already decodes. It is repeated
#: here rather than imported because `core` may not depend on `adapters`; the two are held together
#: by `test_comfyui_assisted_draft`, which asserts they are the same string.
OLLAMA_DRAFT_SCHEMA_ID = "h3.prompt_model.draft_json.v1"


def ollama_draft_format_schema() -> dict[str, object]:
    """The shared draft object, kept at the existing import seam."""
    return draft_schema()


def build_request_payload(
    request: object, options: RequestOptions | None = None
) -> dict[str, object]:
    """Dispatch to a pure wire dialect; transport and consent remain outside core."""
    if not isinstance(request, PromptModelSessionRequest):
        _fail("session_request")
    if options is None:
        options = RequestOptions()
    if not isinstance(options, RequestOptions):
        _fail("request_options")
    options = options_for_metadata(request, options)
    if request.family is PromptModelFamily.OLLAMA:
        return ollama_chat.build(request, options)
    if request.family is PromptModelFamily.REMOTE_ANTHROPIC:
        return anthropic_messages.build(request, options)
    if request.family in OPENAI_COMPATIBLE_FAMILIES:
        return openai_chat.build(request, options)
    _fail("session_family")


@dataclass(frozen=True, slots=True)
class PromptModelAnswer:
    """A completed answer plus the evidence that the model producing it was the pinned one."""

    text: str
    verification: IdentityVerification
    finish_reason: str
    observed_model_id: str = ""

    def __post_init__(self) -> None:
        _text(self.text, MAX_MESSAGE_CHARACTERS, "answer_text", allow_empty=True)
        if not isinstance(self.verification, IdentityVerification):
            _fail("answer_verification")
        _text(self.finish_reason, 64, "answer_finish_reason")
        _text(self.observed_model_id, MAX_IDENTIFIER_CHARACTERS, "answer_model", allow_empty=True)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_SESSION_SCHEMA,
            "characters": len(self.text),
            "verification": self.verification.value,
            "finish_reason": self.finish_reason,
            "observed_model_id": self.observed_model_id,
        }


def read_response_answer(
    family: object, response: object, model_id: object = None
) -> DialectAnswer:
    """Read final-channel text and its observed model through the selected dialect."""
    if family is PromptModelFamily.OLLAMA:
        return ollama_chat.read(response, model_id)
    if family is PromptModelFamily.REMOTE_ANTHROPIC:
        return anthropic_messages.read(response, model_id)
    if family in OPENAI_COMPATIBLE_FAMILIES:
        return openai_chat.read(
            response, model_id, remote=family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
        )
    _fail("session_family")


def parse_response_text(family: object, response: object, model_id: object = None) -> str:
    """Compatibility dispatcher for callers that only consume final text."""
    return read_response_answer(family, response, model_id).text


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """One thing the user can see, and the single closed reason it is or is not usable."""

    identifier: str
    reason: DiscoveryRejection
    metadata: ModelMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, DiscoveryRejection):
            _fail("candidate_reason")
        if self.metadata is not None and not isinstance(self.metadata, ModelMetadata):
            _fail("candidate_metadata")
        # Only the "nothing was there at all" sentinel may name nothing; every other reason is a
        # statement about a specific candidate the user can see.
        empty_allowed = self.reason is DiscoveryRejection.NO_CANDIDATE
        _text(
            self.identifier,
            MAX_IDENTIFIER_CHARACTERS,
            "candidate_identifier",
            allow_empty=empty_allowed,
        )

    @property
    def admitted(self) -> bool:
        return self.reason is DiscoveryRejection.ADMITTED

    def to_wire(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "reason": self.reason.value,
            "metadata": None if self.metadata is None else self.metadata.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class DiscoveryObservation:
    """What an adapter saw, with no judgement attached; `describe_discovery` judges it."""

    identifier: str
    root_present: bool
    duplicate_identifier: bool = False
    projector_expected: bool = False
    projector_present: bool = False

    def __post_init__(self) -> None:
        _text(self.identifier, MAX_IDENTIFIER_CHARACTERS, "observation_identifier")
        for name in (
            "root_present",
            "duplicate_identifier",
            "projector_expected",
            "projector_present",
        ):
            if type(getattr(self, name)) is not bool:
                _fail("observation_flag")


def describe_discovery(
    observations: object, pinned_identifiers: object
) -> tuple[DiscoveryCandidate, ...]:
    """Explain every candidate. Reports ambiguity, resolves none, and reads no filename."""

    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        _fail("discovery_observations")
    entries = tuple(observations)
    if len(entries) > MAX_DISCOVERY_ROWS:
        _fail("discovery_observations")
    if not isinstance(pinned_identifiers, (frozenset, set, tuple, list)):
        _fail("discovery_pinned")
    pinned = frozenset(str(value) for value in pinned_identifiers)

    candidates: list[DiscoveryCandidate] = []
    for entry in entries:
        if not isinstance(entry, DiscoveryObservation):
            _fail("discovery_observations")
        if not entry.root_present:
            reason = DiscoveryRejection.MISSING_ROOT
        elif entry.duplicate_identifier:
            reason = DiscoveryRejection.AMBIGUOUS_FOLDER
        elif entry.projector_expected and not entry.projector_present:
            reason = DiscoveryRejection.UNPAIRED_PROJECTOR
        elif entry.identifier not in pinned:
            reason = DiscoveryRejection.UNPINNED
        else:
            reason = DiscoveryRejection.ADMITTED
        candidates.append(DiscoveryCandidate(identifier=entry.identifier, reason=reason))
    if not candidates:
        return (DiscoveryCandidate(identifier="", reason=DiscoveryRejection.NO_CANDIDATE),)
    return tuple(candidates)


@dataclass(frozen=True, slots=True)
class UnloadDecision:
    """What the arbiter did, and why."""

    disposition: UnloadDisposition
    family: PromptModelFamily
    in_flight: int

    def to_wire(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "family": self.family.value,
            "in_flight": self.in_flight,
        }


@dataclass(slots=True)
class UnloadArbiter:
    """Tracks in-flight requests per family so an unload can never race one.

    Not thread-safe by itself; a caller that shares one across threads holds its own lock. The point
    of the type is the decision, not the synchronisation.
    """

    _in_flight: dict[PromptModelFamily, int] = field(default_factory=dict)
    _loaded: set[PromptModelFamily] = field(default_factory=set)

    def mark_loaded(self, family: PromptModelFamily) -> None:
        if not isinstance(family, PromptModelFamily):
            _fail("arbiter_family")
        self._loaded.add(family)

    def begin_request(self, family: PromptModelFamily) -> None:
        if not isinstance(family, PromptModelFamily):
            _fail("arbiter_family")
        self._loaded.add(family)
        self._in_flight[family] = self._in_flight.get(family, 0) + 1

    def end_request(self, family: PromptModelFamily) -> None:
        if not isinstance(family, PromptModelFamily):
            _fail("arbiter_family")
        current = self._in_flight.get(family, 0)
        if current <= 0:
            _fail("arbiter_underflow")
        self._in_flight[family] = current - 1

    def in_flight(self, family: PromptModelFamily) -> int:
        if not isinstance(family, PromptModelFamily):
            _fail("arbiter_family")
        return self._in_flight.get(family, 0)

    def request_unload(self, family: object) -> UnloadDecision:
        if not isinstance(family, PromptModelFamily):
            _fail("arbiter_family")
        active = self._in_flight.get(family, 0)
        if active > 0:
            return UnloadDecision(
                disposition=UnloadDisposition.DEFERRED_IN_FLIGHT,
                family=family,
                in_flight=active,
            )
        if family not in self._loaded:
            return UnloadDecision(
                disposition=UnloadDisposition.NOT_LOADED, family=family, in_flight=0
            )
        self._loaded.discard(family)
        return UnloadDecision(disposition=UnloadDisposition.PERFORMED, family=family, in_flight=0)


@dataclass(frozen=True, slots=True)
class NativeProbeReport:
    """Capability evidence from the out-of-process probe. Never a performance claim."""

    status: NativeProbeStatus
    runtime_version: str
    detected_backends: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, NativeProbeStatus):
            _fail("probe_status")
        _text(self.runtime_version, 64, "probe_version", allow_empty=True)
        if not isinstance(self.detected_backends, tuple) or len(self.detected_backends) > 8:
            _fail("probe_backends")
        for value in self.detected_backends:
            _text(value, 32, "probe_backends")
        if self.status is not NativeProbeStatus.READY and (
            self.runtime_version or self.detected_backends
        ):
            # A failed probe carries no capability claim at all; letting one through would be the
            # fail-open behaviour this item exists to invert.
            _fail("probe_failed_with_claim")

    @property
    def usable(self) -> bool:
        return self.status is NativeProbeStatus.READY

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_SESSION_SCHEMA,
            "status": self.status.value,
            "runtime_version": self.runtime_version,
            "detected_backends": list(self.detected_backends),
        }


def probe_outcome(report: object) -> PromptModelOutcome:
    """Turn a probe report into the family-blocking outcome it implies."""

    if not isinstance(report, NativeProbeReport):
        _fail("probe_report")
    if report.usable:
        return build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(("status", report.status.value),),
        )
    remediation = (
        PromptModelRemediation.RETRY_LATER
        if report.status is NativeProbeStatus.TIMED_OUT
        else PromptModelRemediation.INSTALL_BACKEND
    )
    return build_prompt_model_outcome(
        PromptModelOutcomeId.BACKEND_ABSENT,
        severity=ValidationSeverity.ERROR,
        remediation=remediation,
        parameters=(("status", report.status.value),),
    )


class PromptModelExchange(Protocol):
    """The bounded request seam a transport implements and a test double replaces."""

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]: ...
