"""M22-03 prompt-model session contract: what a local transport must do, judged in pure code.

The transports themselves live in `adapters/prompt_model_transport.py`, because they open sockets
and start subprocesses. Everything that decides *whether* a request may be issued, *what* the
request looks like, and *how* an answer or a failure is classified lives here, where it can be
tested without a server.

Three decisions are worth stating plainly.

**Identity is re-verified at request time.** A local daemon is a mutable external resource: the
model behind a selected name can be swapped between the moment the user chooses it and the moment
the request is sent. So the live identity is resolved before every request and checked against the
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
from .prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    AdmittedDestination,
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
from .remote_provider_policy import policy_for_profile, remote_output_tokens_from_plan

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

    def __post_init__(self) -> None:
        if not isinstance(self.profile, PromptModelProfile):
            _fail("request_profile")
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


@dataclass(frozen=True, slots=True)
class LiveModelIdentity:
    """What the runtime says it is holding right now, as observed by an adapter."""

    model_id: str
    digest: str | None

    def __post_init__(self) -> None:
        _text(self.model_id, MAX_IDENTIFIER_CHARACTERS, "identity_model_id")
        if self.digest is not None:
            _text(self.digest, MAX_IDENTIFIER_CHARACTERS, "identity_digest")


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    """The result of re-checking the live model against the pinned entry."""

    outcome: PromptModelOutcome
    verification: IdentityVerification | None

    @property
    def admitted(self) -> bool:
        return self.verification is not None


def verify_live_identity(profile: object, observed: object) -> IdentityDecision:
    """Re-tie a live model to its pinned entry, or refuse. Never substitutes a near match."""

    if not isinstance(profile, PromptModelProfile):
        _fail("identity_profile")
    if not isinstance(observed, LiveModelIdentity):
        _fail("identity_observation")

    if observed.model_id != profile.model_id:
        return IdentityDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.MODEL_MISSING,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(("expected_model_id", profile.model_id),),
            ),
            verification=None,
        )
    if observed.digest is None:
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
    if observed.digest != profile.model_digest:
        return IdentityDecision(
            outcome=build_prompt_model_outcome(
                PromptModelOutcomeId.DIGEST_MISMATCH,
                severity=ValidationSeverity.ERROR,
                remediation=PromptModelRemediation.SELECT_MODEL,
                parameters=(("model_id", profile.model_id),),
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
    """The exact JSON shape the local model is constrained to emit (M22-13).

    This is the `h3.prompt_model.draft_json.v1` object the accepted decoder already requires --
    both keys, no others -- expressed as a JSON Schema so the server constrains generation to it
    instead of the decoder discovering the violation afterwards. `additionalProperties` is false,
    so a model that wants to add a `thinking`, `reasoning` or `tool_calls` key has to break the
    grammar rather than have the extra key quietly dropped downstream. The schema id is spelled as
    a single-valued `enum` rather than `const` because that is the form the grammar conversion
    reliably understands.

    A fresh dict per call: the value is serialized straight onto the wire, so it must be plain
    JSON-encodable, and no caller may mutate a shared constant that decides what the server is
    allowed to answer.
    """

    return {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "enum": [OLLAMA_DRAFT_SCHEMA_ID]},
            "prompt_text": {"type": "string"},
        },
        "required": ["schema", "prompt_text"],
        "additionalProperties": False,
    }


def build_request_payload(request: object) -> dict[str, object]:
    """Render the wire payload for the family. One shape per family, nothing guessed."""

    if not isinstance(request, PromptModelSessionRequest):
        _fail("session_request")
    if request.family is PromptModelFamily.OLLAMA:
        # M22-13. The curated local model declares `thinking`, `tools` and `vision` alongside
        # `completion`, so every one of those has to be switched off explicitly rather than left to
        # the server's default. A capable model that was never told not to think will think, and a
        # reasoning preamble is not a draft.
        if request.image_payloads:
            _fail("session_media_unsupported")
        messages = [
            {"role": message.role.value, "content": message.text} for message in request.messages
        ]
        return {
            "model": request.profile.model_id,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": ollama_draft_format_schema(),
            "keep_alive": 0,
            "options": {
                "num_ctx": request.plan.context_tokens,
                "num_predict": request.plan.reserved_output_tokens,
            },
        }
    if request.family is PromptModelFamily.REMOTE_ANTHROPIC:
        if request.image_payloads:
            _fail("session_media_unsupported")
        system_indexes = [
            index
            for index, message in enumerate(request.messages)
            if message.role is PromptModelRole.SYSTEM
        ]
        if system_indexes != [0]:
            # IMPORTANT: Anthropic's system instruction is top-level. Moving or merging system
            # turns would change conversation authority, so only one leading system turn is valid.
            _fail("session_system_message")
        anthropic_messages: list[dict[str, str]] = []
        for message in request.messages[1:]:
            if message.role not in {PromptModelRole.USER, PromptModelRole.ASSISTANT}:
                _fail("session_message_role")
            anthropic_messages.append({"role": message.role.value, "content": message.text})
        if not anthropic_messages:
            _fail("session_messages")
        policy = policy_for_profile(request.profile)
        output_tokens = remote_output_tokens_from_plan(policy, request.plan.reserved_output_tokens)
        anthropic_payload: dict[str, object] = {
            "model": request.profile.model_id,
            "system": request.messages[0].text,
            "messages": anthropic_messages,
            "max_tokens": output_tokens,
            "stream": False,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": ollama_draft_format_schema(),
                }
            },
        }
        return anthropic_payload
    if request.family in OPENAI_COMPATIBLE_FAMILIES:
        chat: list[dict[str, object]] = []
        for index, message in enumerate(request.messages):
            if index == len(request.messages) - 1 and request.image_payloads:
                parts: list[dict[str, object]] = [{"type": "text", "text": message.text}]
                for image_payload in request.image_payloads:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_payload}"},
                        }
                    )
                chat.append({"role": message.role.value, "content": parts})
            else:
                chat.append({"role": message.role.value, "content": message.text})
        request_payload: dict[str, object] = {
            "model": request.profile.model_id,
            "messages": chat,
            "stream": False,
        }
        if request.family is PromptModelFamily.LOOPBACK_SERVER:
            request_payload["max_tokens"] = request.plan.reserved_output_tokens
            return request_payload
        if request.image_payloads:
            _fail("session_media_unsupported")
        policy = policy_for_profile(request.profile)
        output_tokens = remote_output_tokens_from_plan(policy, request.plan.reserved_output_tokens)
        request_payload.update(
            {
                "max_completion_tokens": output_tokens,
                "modalities": ["text"],
                "n": 1,
                "tool_choice": "none",
                "reasoning_effort": policy.reasoning_effort,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "h3_prompt_model_draft",
                        "strict": True,
                        "schema": ollama_draft_format_schema(),
                    },
                },
            }
        )
        if policy.store is not None:
            request_payload["store"] = policy.store
        return request_payload
    _fail("session_family")


@dataclass(frozen=True, slots=True)
class PromptModelAnswer:
    """A completed answer plus the evidence that the model producing it was the pinned one."""

    text: str
    verification: IdentityVerification
    finish_reason: str

    def __post_init__(self) -> None:
        _text(self.text, MAX_MESSAGE_CHARACTERS, "answer_text", allow_empty=True)
        if not isinstance(self.verification, IdentityVerification):
            _fail("answer_verification")
        _text(self.finish_reason, 64, "answer_finish_reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_SESSION_SCHEMA,
            "characters": len(self.text),
            "verification": self.verification.value,
            "finish_reason": self.finish_reason,
        }


def parse_response_text(family: object, response: object, model_id: object = None) -> str:
    """Read the answer out of a family's response shape, refusing anything else.

    `model_id` is the exact id the request named. Supplying it lets qualified remote lanes and the
    local Ollama lane refuse an answer attributed to a different model; it remains optional only
    for legacy loopback OpenAI-compatible responses that expose no equivalent guarantee.
    """

    if not isinstance(family, PromptModelFamily):
        _fail("session_family")
    if not isinstance(response, Mapping):
        _fail("response_shape")
    if family is PromptModelFamily.OLLAMA:
        # M22-13. Everything below is a property the request explicitly asked the server for. A
        # response that does not have it is not a slightly-off answer to this request; it is an
        # answer to a different one, and reading a draft out of it would be reading a stranger.
        if response.get("done") is not True:
            # An unfinished response can still carry plausible content. Refusing it here is what
            # stops a truncated draft from being presented as a complete proposal.
            _fail("response_incomplete")
        if model_id is not None and response.get("model") != model_id:
            _fail("response_model")
        message = response.get("message")
        if not isinstance(message, Mapping):
            _fail("response_shape")
        # `think:false` was sent, no tool was offered and no image was sent, so any of these
        # appearing means the server did not honour the request that was actually made.
        if message.get("thinking") not in (None, ""):
            _fail("response_reasoning")
        if message.get("tool_calls"):
            _fail("response_tool_call")
        if message.get("images"):
            _fail("response_media")
        content = message.get("content")
        if not isinstance(content, str):
            _fail("response_shape")
        return _text(content, MAX_MESSAGE_CHARACTERS, "response_text", allow_empty=True)
    if family is PromptModelFamily.REMOTE_ANTHROPIC:
        if response.get("type") != "message":
            _fail("response_shape")
        if response.get("role") != "assistant":
            _fail("response_role")
        if not isinstance(model_id, str) or response.get("model") != model_id:
            _fail("response_model")
        if response.get("stop_reason") != "end_turn":
            _fail("response_incomplete")
        content = response.get("content")
        if (
            not isinstance(content, Sequence)
            or isinstance(content, (str, bytes))
            or len(content) != 1
        ):
            _fail("response_shape")
        block = content[0]
        if not isinstance(block, Mapping) or set(block) != {"type", "text"}:
            _fail("response_unrequested_channel")
        if block.get("type") != "text":
            _fail("response_unrequested_channel")
        return _text(block.get("text"), MAX_MESSAGE_CHARACTERS, "response_text", allow_empty=True)
    if family in OPENAI_COMPATIBLE_FAMILIES:
        choices = response.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            _fail("response_shape")
        if family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE:
            if not isinstance(model_id, str) or response.get("model") != model_id:
                _fail("response_model")
            if len(choices) != 1:
                _fail("response_choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            _fail("response_shape")
        if family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE:
            if first.get("index") != 0 or first.get("finish_reason") != "stop":
                _fail("response_incomplete")
        message = first.get("message")
        if not isinstance(message, Mapping):
            _fail("response_shape")
        if family is PromptModelFamily.REMOTE_OPENAI_COMPATIBLE:
            if message.get("role") != "assistant":
                _fail("response_role")
            # SECURITY: text-only, no-tools execution cannot accept a refusal or hidden side
            # channel as if it were the requested draft payload.
            for forbidden in ("refusal", "tool_calls", "function_call", "audio", "reasoning"):
                if message.get(forbidden) not in (None, "", [], {}):
                    _fail("response_unrequested_channel")
        content = message.get("content")
        if not isinstance(content, str):
            _fail("response_shape")
        return _text(content, MAX_MESSAGE_CHARACTERS, "response_text", allow_empty=True)
    _fail("session_family")


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """One thing the user can see, and the single closed reason it is or is not usable."""

    identifier: str
    reason: DiscoveryRejection

    def __post_init__(self) -> None:
        if not isinstance(self.reason, DiscoveryRejection):
            _fail("candidate_reason")
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
        return {"identifier": self.identifier, "reason": self.reason.value}


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
