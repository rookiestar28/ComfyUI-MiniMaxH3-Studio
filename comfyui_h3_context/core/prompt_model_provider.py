"""M22-01 prompt-model provider contract with one audited egress chokepoint.

This module is the single authority for *which* prompt-model execution families exist, what each
one must declare about itself before it can be used, and what an outbound destination has to look
like before any transport in `M22-03`/`M22-04` is allowed to open a connection to it. It owns no
transport, performs no network access, and selects no provider: a caller names a family explicitly
or nothing happens at all.

It reuses, rather than restates, the accepted `provider_setup` vocabulary -- `ProviderDestination
Class`, `ProviderTransferBoundary` and `ProviderConsentStatus` -- so a prompt-model family inherits
the disclosure and consent semantics already accepted for H3 providers instead of growing a second,
divergent set of words for the same ideas.

Two chokepoints exist in this package and they do not overlap. `security.validate_remote_url` admits
*media fetches*: HTTPS only, an explicit host allowlist, globally reachable addresses only.
`admit_egress_destination` below admits *prompt-model endpoints*, which are usually loopback and
usually plaintext, and which are therefore governed by different rules. Neither is a fallback for
the other, and the instance-metadata blocklist is defined here exactly once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date
from enum import Enum
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn
from urllib.parse import unquote, urlsplit

from .contracts import ValidationSeverity
from .provider_setup import (
    ProviderConsentStatus,
    ProviderDestinationClass,
    ProviderTransferBoundary,
)
from .security import SecurityPolicyError, normalise_url_host

PROMPT_MODEL_PROVIDER_SCHEMA = "h3-context-prompt-model-provider/1"
PROMPT_MODEL_CATALOG_V1_SCHEMA = "h3.prompt_model.profiles.v1"
PROMPT_MODEL_CATALOG_V2_SCHEMA = "h3.prompt_model.profiles.v2"
PROMPT_MODEL_CATALOG_V3_SCHEMA = "h3.prompt_model.profiles.v3"
PROMPT_MODEL_CATALOG_V4_SCHEMA = "h3.prompt_model.profiles.v4"
# M22-13: v3 is the first version that can carry an executed-qualification claim. v1 and v2 stay
# decodable on purpose -- an older shipped catalog must not become undecodable -- but neither can
# express `qualified`, so a v1/v2 payload can never smuggle execution authority past this decoder.
PROMPT_MODEL_CATALOG_V5_SCHEMA = "h3.prompt_model.profiles.v5"
PROMPT_MODEL_CATALOG_V6_SCHEMA = "h3.prompt_model.profiles.v6"
PROMPT_MODEL_CATALOG_SCHEMA = PROMPT_MODEL_CATALOG_V6_SCHEMA
PROMPT_MODEL_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "contracts" / "prompt_model_profiles_v6.json"
)
REMOTE_QUALIFICATION_RESULT_SCHEMA = "h3.remote.prompt_model.qualification.v1"
REMOTE_QUALIFICATION_RECEIPT_SCHEMA = "h3.remote.prompt_model.qualification_receipt.v1"

MAX_ENDPOINT_CHARACTERS = 2_048
MAX_CATALOG_BYTES = 65_536
MAX_CATALOG_PROFILES = 8
MAX_OUTCOME_PARAMETERS = 32
MAX_OUTCOME_PARAMETER_CHARACTERS = 128
MAX_PROVIDER_DETAIL_CHARACTERS = 512
MAX_REQUEST_BYTES_CEILING = 64 * 1024 * 1024
MAX_TOKEN_CEILING = 4_194_304

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_PARAMETER_KEY = re.compile(r"[a-z][a-z0-9_]{0,47}")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_API_KEY = re.compile(r"(?i)\b(?:sk|rk|pk|api)[-_][A-Za-z0-9_-]{12,}")
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{32,}")
_WHITESPACE = re.compile(r"\s+")

# The instance-metadata endpoints every major cloud exposes to anything that can make an outbound
# request from inside the host. A prompt-model endpoint is never legitimately one of these, and a
# request that reaches one leaks the host's own credentials, so the blocklist is enforced before the
# destination class is even computed. Defined here once; no other module may restate it.
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "100.100.100.200",
        "fd00:ec2::254",
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
    }
)


class PromptModelFamily(str, Enum):
    """Closed set of prompt-model execution families; membership is never inferred."""

    IN_PROCESS_GGUF = "in_process_gguf"
    LOOPBACK_SERVER = "loopback_server"
    OLLAMA = "ollama"
    REMOTE_OPENAI_COMPATIBLE = "remote_openai_compatible"
    REMOTE_ANTHROPIC = "remote_anthropic"


class PromptModelDialect(str, Enum):
    """Wire protocol is declared by profile identity and never inferred from a URL."""

    LEGACY = "legacy"
    OLLAMA_CHAT = "ollama_chat"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class PromptModelQualificationState(str, Enum):
    """Catalog acceptance and executed provider qualification are different claims."""

    LEGACY_UNQUALIFIED = "legacy_unqualified"
    CATALOG_ONLY = "catalog_only"
    QUALIFIED = "qualified"


class RemoteQualificationBasis(str, Enum):
    """What a live run actually proved, said out loud rather than inferred from zeros.

    M22-18. `COMPLETION` is the original bar: the provider produced a draft and billed for it.
    `REACHABILITY` is weaker and deliberately separate: the service was reached, authenticated,
    routed to the pinned model, and answered about a real content request that it declined to
    bill. The two are disjoint by outcome -- `prompt_model.ok` against `prompt_model.quota` -- so
    a run can never slide from the stronger claim to the weaker one by failing a check.
    """

    COMPLETION = "completion"
    REACHABILITY = "reachability"


class PromptModelMediaKind(str, Enum):
    """Media a family accepts as request content."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class PromptModelCredentialSource(str, Enum):
    """Where a credential may come from. Session-only, because indirection hides the holder."""

    NONE = "none"
    SESSION_ONLY = "session_only"


class EgressRejection(str, Enum):
    """Closed reasons a destination is refused; each is a distinct, actionable class."""

    LENGTH = "length"
    MALFORMED = "malformed"
    SCHEME = "unsupported_scheme"
    EMBEDDED_CREDENTIAL = "embedded_credential"
    QUERY = "query_present"
    FRAGMENT = "fragment_present"
    HOST_MISSING = "host_missing"
    PATH_TRAVERSAL = "path_traversal"
    METADATA_HOST = "metadata_host"
    LINK_LOCAL = "link_local"
    DESTINATION_CLASS = "destination_class_mismatch"
    PLAINTEXT_PUBLIC = "plaintext_to_public_host"
    IN_PROCESS_FAMILY = "in_process_family_has_no_egress"


class PromptModelOutcomeId(str, Enum):
    """Stable outcome identifiers.

    Cancellation, timeout, authentication, quota, moderation and unsupported media are
    separate classes because they call for separate responses; collapsing any pair of them
    into one code would make the difference unrecoverable downstream.
    """

    OK = "prompt_model.ok"
    CANCELLED = "prompt_model.cancelled"
    TIMEOUT = "prompt_model.timeout"
    AUTHENTICATION = "prompt_model.authentication"
    QUOTA = "prompt_model.quota"
    MODERATED = "prompt_model.moderated"
    UNSUPPORTED_MEDIA = "prompt_model.unsupported_media"
    CONTEXT_EXCEEDED = "prompt_model.context_exceeded"
    REQUEST_TOO_LARGE = "prompt_model.request_too_large"
    INSUFFICIENT_MEMORY = "prompt_model.insufficient_memory"
    PROVIDER_MANAGED_SETTING = "prompt_model.provider_managed_setting"
    TRUNCATED_REASONING = "prompt_model.truncated_reasoning"
    ESTIMATE_UNAVAILABLE = "prompt_model.estimate_unavailable"
    BACKEND_ABSENT = "prompt_model.backend_absent"
    MODEL_MISSING = "prompt_model.model_missing"
    DIGEST_MISMATCH = "prompt_model.digest_mismatch"
    CAPABILITY_MISMATCH = "prompt_model.capability_mismatch"
    PROFILE_NOT_QUALIFIED = "prompt_model.profile_not_qualified"
    CONSENT_REQUIRED = "prompt_model.consent_required"
    EGRESS_REFUSED = "prompt_model.egress_refused"
    TRANSPORT = "prompt_model.transport"
    MALFORMED_RESPONSE = "prompt_model.malformed_response"
    PROVIDER_ERROR = "prompt_model.provider_error"
    DRAFT_EMPTY = "prompt_model.draft_empty"
    DRAFT_TRUNCATED = "prompt_model.draft_truncated"
    DRAFT_INSTRUCTION_REFUSED = "prompt_model.draft_instruction_refused"
    REPAIR_REAUDIT_FAILED = "prompt_model.repair_reaudit_failed"
    REPAIR_REFERENCE_DRIFT = "prompt_model.repair_reference_drift"
    REPAIR_USER_TEXT_ALTERED = "prompt_model.repair_user_text_altered"
    REPAIR_OUTPUT_TRUNCATED = "prompt_model.repair_output_truncated"
    CONSENT_REVOKED = "prompt_model.consent_revoked"
    NETWORK_NOT_PERMITTED = "prompt_model.network_not_permitted"
    UPLOAD_NOT_CONSENTED = "prompt_model.upload_not_consented"
    PAYMENT_REQUIRED = "prompt_model.payment_required"
    PERMISSION_DENIED = "prompt_model.permission_denied"
    RATE_LIMITED = "prompt_model.rate_limited"
    REDIRECT_REFUSED = "prompt_model.redirect_refused"
    DESTINATION_UNRESOLVED = "prompt_model.destination_unresolved"


PROMPT_MODEL_OUTCOME_IDS: frozenset[str] = frozenset(item.value for item in PromptModelOutcomeId)


class PromptModelRemediation(str, Enum):
    """What the operator can actually do about an outcome."""

    NONE = "none"
    RETRY_LATER = "retry_later"
    REDUCE_REQUEST = "reduce_request"
    REVIEW_CREDENTIAL = "review_credential"
    GRANT_CONSENT = "grant_consent"
    INSTALL_BACKEND = "install_backend"
    SELECT_MODEL = "select_model"
    CORRECT_ENDPOINT = "correct_endpoint"
    CHANGE_MEDIA = "change_media"


_RETRYABLE_OUTCOMES = frozenset(
    {
        PromptModelOutcomeId.TIMEOUT,
        PromptModelOutcomeId.QUOTA,
        PromptModelOutcomeId.RATE_LIMITED,
        PromptModelOutcomeId.TRANSPORT,
        PromptModelOutcomeId.PROVIDER_ERROR,
    }
)

#: What an operator can do about each failure identity, in one place.
#:
#: Session execution and readiness both use this table. An identity absent here has no safe
#: remediation; callers must not invent a generic action from provider prose.
FAILURE_REMEDIATIONS: Mapping[PromptModelOutcomeId, PromptModelRemediation] = MappingProxyType(
    {
        PromptModelOutcomeId.BACKEND_ABSENT: PromptModelRemediation.INSTALL_BACKEND,
        PromptModelOutcomeId.MODEL_MISSING: PromptModelRemediation.SELECT_MODEL,
        PromptModelOutcomeId.QUOTA: PromptModelRemediation.RETRY_LATER,
        PromptModelOutcomeId.RATE_LIMITED: PromptModelRemediation.RETRY_LATER,
        PromptModelOutcomeId.TIMEOUT: PromptModelRemediation.RETRY_LATER,
        PromptModelOutcomeId.TRANSPORT: PromptModelRemediation.RETRY_LATER,
        PromptModelOutcomeId.AUTHENTICATION: PromptModelRemediation.REVIEW_CREDENTIAL,
        PromptModelOutcomeId.REQUEST_TOO_LARGE: PromptModelRemediation.REDUCE_REQUEST,
        PromptModelOutcomeId.UNSUPPORTED_MEDIA: PromptModelRemediation.CHANGE_MEDIA,
        PromptModelOutcomeId.EGRESS_REFUSED: PromptModelRemediation.CORRECT_ENDPOINT,
        PromptModelOutcomeId.DIGEST_MISMATCH: PromptModelRemediation.SELECT_MODEL,
        PromptModelOutcomeId.CAPABILITY_MISMATCH: PromptModelRemediation.SELECT_MODEL,
        PromptModelOutcomeId.CONSENT_REQUIRED: PromptModelRemediation.GRANT_CONSENT,
    }
)


def remediation_for(outcome_id: object) -> PromptModelRemediation:
    """The remediation for one identity, or `NONE`. Never guesses from a message."""

    if not isinstance(outcome_id, PromptModelOutcomeId):
        return PromptModelRemediation.NONE
    return FAILURE_REMEDIATIONS.get(outcome_id, PromptModelRemediation.NONE)


class PromptModelContractError(ValueError):
    """Closed, content-free M22-01 contract failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PromptModelEgressError(PromptModelContractError):
    """A destination refused by the single egress chokepoint."""

    def __init__(self, rejection: EgressRejection) -> None:
        self.rejection = rejection
        super().__init__("egress_refused")


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _refuse(rejection: EgressRejection) -> NoReturn:
    raise PromptModelEgressError(rejection)


@dataclass(frozen=True, slots=True)
class PromptModelRoute:
    """The accepted-vocabulary route a family is pinned to. One row per family, no exceptions."""

    family: PromptModelFamily
    destination: ProviderDestinationClass
    transfer_boundary: ProviderTransferBoundary
    preflight_required: bool
    consent_required: bool

    @property
    def local_only(self) -> bool:
        return self.destination is not ProviderDestinationClass.INTERNET


PROMPT_MODEL_FAMILY_MATRIX: Mapping[PromptModelFamily, PromptModelRoute] = {
    PromptModelFamily.IN_PROCESS_GGUF: PromptModelRoute(
        family=PromptModelFamily.IN_PROCESS_GGUF,
        destination=ProviderDestinationClass.IN_PROCESS,
        transfer_boundary=ProviderTransferBoundary.IN_PROCESS,
        preflight_required=True,
        consent_required=False,
    ),
    PromptModelFamily.LOOPBACK_SERVER: PromptModelRoute(
        family=PromptModelFamily.LOOPBACK_SERVER,
        destination=ProviderDestinationClass.LOOPBACK_HTTP,
        transfer_boundary=ProviderTransferBoundary.LOCAL_SERVER_PROCESS,
        preflight_required=True,
        consent_required=False,
    ),
    PromptModelFamily.OLLAMA: PromptModelRoute(
        family=PromptModelFamily.OLLAMA,
        destination=ProviderDestinationClass.LOOPBACK_HTTP,
        transfer_boundary=ProviderTransferBoundary.OLLAMA_PROCESS,
        preflight_required=True,
        consent_required=False,
    ),
    PromptModelFamily.REMOTE_OPENAI_COMPATIBLE: PromptModelRoute(
        family=PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        destination=ProviderDestinationClass.INTERNET,
        transfer_boundary=ProviderTransferBoundary.REMOTE_UPLOAD,
        preflight_required=True,
        consent_required=True,
    ),
    PromptModelFamily.REMOTE_ANTHROPIC: PromptModelRoute(
        family=PromptModelFamily.REMOTE_ANTHROPIC,
        destination=ProviderDestinationClass.INTERNET,
        transfer_boundary=ProviderTransferBoundary.REMOTE_UPLOAD,
        preflight_required=True,
        consent_required=True,
    ),
}


def route_for_family(family: object) -> PromptModelRoute:
    """Return the pinned route for a family, refusing anything that is not a family value."""

    if not isinstance(family, PromptModelFamily):
        _fail("unknown_family")
    route = PROMPT_MODEL_FAMILY_MATRIX.get(family)
    if route is None:  # pragma: no cover - the matrix is asserted total by tests
        _fail("unknown_family")
    return route


def admit_prompt_model_execution(family: object, consent: object) -> None:
    """Admit execution for an explicitly named family, or refuse. Never selects a family."""

    route = route_for_family(family)
    if not isinstance(consent, ProviderConsentStatus):
        _fail("unknown_consent")
    if route.consent_required:
        if consent is not ProviderConsentStatus.GRANTED:
            _fail("consent_required")
        return
    if consent is not ProviderConsentStatus.NOT_REQUIRED:
        _fail("consent_not_applicable")


_CAPABILITY_KEYS = frozenset(
    {
        "family",
        "accepted_media",
        "provider_managed_context",
        "provider_managed_kv_cache",
        "max_request_bytes",
        "max_context_tokens",
        "max_output_tokens",
        "streaming",
        "local_only",
        "requires_credential",
    }
)


@dataclass(frozen=True, slots=True)
class PromptModelCapabilities:
    """Everything a family must say about itself. No field has a default, so none can be omitted."""

    family: PromptModelFamily
    accepted_media: frozenset[PromptModelMediaKind]
    provider_managed_context: bool
    provider_managed_kv_cache: bool
    max_request_bytes: int
    max_context_tokens: int
    max_output_tokens: int
    streaming: bool
    local_only: bool
    requires_credential: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "accepted_media": sorted(item.value for item in self.accepted_media),
            "provider_managed_context": self.provider_managed_context,
            "provider_managed_kv_cache": self.provider_managed_kv_cache,
            "max_request_bytes": self.max_request_bytes,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "streaming": self.streaming,
            "local_only": self.local_only,
            "requires_credential": self.requires_credential,
        }


def _flag(values: Mapping[str, object], key: str) -> bool:
    value = values[key]
    if type(value) is not bool:
        _fail("capabilities_flag")
    return value


def _bounded_int(values: Mapping[str, object], key: str, ceiling: int) -> int:
    value = values[key]
    if type(value) is not int or not 1 <= value <= ceiling:
        _fail("capabilities_bound")
    return value


def build_prompt_model_capabilities(values: object) -> PromptModelCapabilities:
    """Decode a capability declaration. An under-declared family cannot be built at all."""

    if not isinstance(values, Mapping):
        _fail("capabilities_shape")
    keys = set(values)
    if keys - _CAPABILITY_KEYS:
        _fail("capabilities_unknown_key")
    if _CAPABILITY_KEYS - keys:
        _fail("capabilities_incomplete")

    raw_family = values["family"]
    known_families = {item.value for item in PromptModelFamily}
    if not isinstance(raw_family, str) or raw_family not in known_families:
        _fail("capabilities_family")
    family = PromptModelFamily(raw_family)
    route = route_for_family(family)

    raw_media = values["accepted_media"]
    if not isinstance(raw_media, list | tuple) or not raw_media:
        _fail("capabilities_media")
    known = {item.value for item in PromptModelMediaKind}
    for item in raw_media:
        if not isinstance(item, str) or item not in known:
            _fail("capabilities_media_kind")
    media = frozenset(PromptModelMediaKind(item) for item in raw_media)
    if PromptModelMediaKind.TEXT not in media:
        _fail("capabilities_media")

    managed_context = _flag(values, "provider_managed_context")
    managed_cache = _flag(values, "provider_managed_kv_cache")
    if family is PromptModelFamily.IN_PROCESS_GGUF and (managed_context or managed_cache):
        # There is no second process to own either, so claiming otherwise is a false declaration.
        _fail("capabilities_managed_state")

    local_only = _flag(values, "local_only")
    if local_only is not route.local_only:
        _fail("capabilities_locality")

    requires_credential = _flag(values, "requires_credential")
    if requires_credential and route.local_only:
        _fail("capabilities_credential")

    return PromptModelCapabilities(
        family=family,
        accepted_media=media,
        provider_managed_context=managed_context,
        provider_managed_kv_cache=managed_cache,
        max_request_bytes=_bounded_int(values, "max_request_bytes", MAX_REQUEST_BYTES_CEILING),
        max_context_tokens=_bounded_int(values, "max_context_tokens", MAX_TOKEN_CEILING),
        max_output_tokens=_bounded_int(values, "max_output_tokens", MAX_TOKEN_CEILING),
        streaming=_flag(values, "streaming"),
        local_only=local_only,
        requires_credential=requires_credential,
    )


_ADMISSION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class AdmittedDestination:
    """A destination that passed the chokepoint. Only `admit_egress_destination` can build one.

    A transport therefore cannot be handed a raw endpoint string: the admission is carried in the
    type, which is what makes "every transport uses the chokepoint" a structural fact rather than a
    convention someone has to remember.
    """

    family: PromptModelFamily
    scheme: str
    host: str
    port: int
    path: str
    destination: ProviderDestinationClass
    loopback: bool
    admission: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.admission is not _ADMISSION_TOKEN:
            _fail("destination_not_admitted")

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    def to_wire(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "destination": self.destination.value,
            "loopback": self.loopback,
        }


def _host_is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _host_is_private(host: str) -> bool:
    try:
        literal = ip_address(host)
    except ValueError:
        return False
    return literal.is_private and not literal.is_loopback and not literal.is_link_local


def _host_is_link_local(host: str) -> bool:
    try:
        return ip_address(host).is_link_local
    except ValueError:
        return False


def admit_egress_destination(family: object, endpoint: object) -> AdmittedDestination:
    """The one place a prompt-model destination is judged. Every transport must come through here.

    No DNS lookup and no connection happens: this is a syntactic and policy judgement only, so it
    is deterministic, offline, and safe to run while decoding a catalog.
    """

    route = route_for_family(family)
    admitted_family = route.family
    if route.destination is ProviderDestinationClass.IN_PROCESS:
        _refuse(EgressRejection.IN_PROCESS_FAMILY)
    if not isinstance(endpoint, str) or not endpoint:
        _refuse(EgressRejection.MALFORMED)
    if len(endpoint) > MAX_ENDPOINT_CHARACTERS:
        _refuse(EgressRejection.LENGTH)
    if endpoint != endpoint.strip() or "\\" in endpoint:
        _refuse(EgressRejection.MALFORMED)
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in endpoint):
        _refuse(EgressRejection.MALFORMED)

    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError:
        _refuse(EgressRejection.MALFORMED)

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        _refuse(EgressRejection.SCHEME)
    if username is not None or password is not None:
        _refuse(EgressRejection.EMBEDDED_CREDENTIAL)
    if parsed.query:
        _refuse(EgressRejection.QUERY)
    if parsed.fragment:
        _refuse(EgressRejection.FRAGMENT)
    if not hostname:
        _refuse(EgressRejection.HOST_MISSING)
    if port is not None and not 1 <= port <= 65535:
        _refuse(EgressRejection.MALFORMED)

    try:
        host = normalise_url_host(hostname)
    except SecurityPolicyError:
        _refuse(EgressRejection.HOST_MISSING)

    if host in _METADATA_HOSTS:
        _refuse(EgressRejection.METADATA_HOST)
    if _host_is_link_local(host):
        _refuse(EgressRejection.LINK_LOCAL)

    loopback = _host_is_loopback(host)
    observed = (
        ProviderDestinationClass.LOOPBACK_HTTP if loopback else ProviderDestinationClass.INTERNET
    )
    if observed is not route.destination:
        # A family declares where it goes; an endpoint that goes somewhere else is not a variant of
        # that family, it is a different one, and choosing it silently is exactly what is forbidden.
        _refuse(EgressRejection.DESTINATION_CLASS)
    if scheme == "http" and not loopback and not _host_is_private(host):
        _refuse(EgressRejection.PLAINTEXT_PUBLIC)

    path = parsed.path or "/"
    decoded = unquote(path)
    if "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/")):
        _refuse(EgressRejection.PATH_TRAVERSAL)

    return AdmittedDestination(
        family=admitted_family,
        scheme=scheme,
        host=host,
        port=port if port is not None else (443 if scheme == "https" else 80),
        path=path,
        destination=observed,
        loopback=loopback,
        admission=_ADMISSION_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class PromptModelCredentialRef:
    """A credential source marker; the compatibility hint field must remain empty."""

    source: PromptModelCredentialSource
    last_four: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, PromptModelCredentialSource):
            _fail("credential_source")
        if not isinstance(self.last_four, str):
            _fail("credential_hint")
        if self.last_four:
            _fail("credential_hint")

    @classmethod
    def from_wire(cls, values: object) -> PromptModelCredentialRef:
        if not isinstance(values, Mapping) or set(values) != {"source", "last_four"}:
            _fail("credential_shape")
        raw = values["source"]
        if not isinstance(raw, str) or raw not in {
            item.value for item in PromptModelCredentialSource
        }:
            # Anything that resolves a secret elsewhere -- an environment variable, a file, a
            # keyring entry -- hides who is holding it and for how long, so it is not expressible.
            _fail("credential_source")
        hint = values["last_four"]
        if not isinstance(hint, str):
            _fail("credential_hint")
        return cls(source=PromptModelCredentialSource(raw), last_four=hint)

    def to_wire(self) -> dict[str, object]:
        return {"source": self.source.value, "last_four": self.last_four}


@dataclass(frozen=True, slots=True)
class UntrustedProviderDetail:
    """Third-party text, carried as attributed evidence and never as product copy."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            _fail("provider_detail")

    @classmethod
    def from_provider_text(cls, value: object) -> UntrustedProviderDetail:
        if not isinstance(value, str):
            _fail("provider_detail")
        cleaned = "".join(char if 0x20 <= ord(char) != 0x7F else " " for char in value)
        cleaned = _BEARER.sub("Bearer [redacted]", cleaned)
        cleaned = _API_KEY.sub("[redacted]", cleaned)
        cleaned = _LONG_RUN.sub("[redacted]", cleaned)
        cleaned = _WHITESPACE.sub(" ", cleaned).strip()
        return cls(text=cleaned[:MAX_PROVIDER_DETAIL_CHARACTERS])

    def to_wire(self) -> dict[str, object]:
        return {
            "source": "provider",
            "trust": "untrusted",
            "renderable_as_product_copy": False,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class PromptModelOutcome:
    """A localisable outcome: an identifier plus typed parameters, with no English sentence.

    Nothing here is a display string. A consumer renders `outcome_id` in its own locale using
    `parameters`; the provider's own words, if carried at all, arrive separately and marked.
    """

    outcome_id: PromptModelOutcomeId
    severity: ValidationSeverity
    remediation: PromptModelRemediation
    parameters: tuple[tuple[str, str | int | bool], ...]
    provider_detail: UntrustedProviderDetail | None

    @property
    def retryable(self) -> bool:
        return self.outcome_id in _RETRYABLE_OUTCOMES

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PROMPT_MODEL_PROVIDER_SCHEMA,
            "outcome_id": self.outcome_id.value,
            "severity": self.severity.value,
            "remediation": self.remediation.value,
            "retryable": self.retryable,
            "parameters": {key: value for key, value in self.parameters},
            "untrusted_provider_detail": (
                None if self.provider_detail is None else self.provider_detail.to_wire()
            ),
        }


def _outcome_parameters(
    parameters: object,
) -> tuple[tuple[str, str | int | bool], ...]:
    if not isinstance(parameters, Sequence) or isinstance(parameters, str | bytes):
        _fail("outcome_parameters")
    entries = tuple(parameters)
    if len(entries) > MAX_OUTCOME_PARAMETERS:
        _fail("outcome_parameters")
    seen: set[str] = set()
    result: list[tuple[str, str | int | bool]] = []
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            _fail("outcome_parameters")
        key, value = entry
        if not isinstance(key, str) or _PARAMETER_KEY.fullmatch(key) is None or key in seen:
            _fail("outcome_parameters")
        seen.add(key)
        if type(value) is bool or type(value) is int:
            if type(value) is int and not -(2**53) < int(value) < 2**53:
                _fail("outcome_parameters")
        elif type(value) is str:
            if len(value) > MAX_OUTCOME_PARAMETER_CHARACTERS:
                _fail("outcome_parameters")
        else:
            _fail("outcome_parameters")
        result.append((key, value))
    return tuple(result)


def build_prompt_model_outcome(
    outcome_id: object,
    *,
    severity: object,
    remediation: object,
    parameters: object,
    provider_detail: object = None,
) -> PromptModelOutcome:
    """Build a closed-vocabulary outcome. An identifier outside the registry cannot be emitted."""

    if not isinstance(outcome_id, PromptModelOutcomeId):
        _fail("unknown_outcome")
    if not isinstance(severity, ValidationSeverity):
        _fail("unknown_severity")
    if not isinstance(remediation, PromptModelRemediation):
        _fail("unknown_remediation")
    detail: UntrustedProviderDetail | None = None
    if provider_detail is not None:
        detail = (
            provider_detail
            if isinstance(provider_detail, UntrustedProviderDetail)
            else UntrustedProviderDetail.from_provider_text(provider_detail)
        )
    return PromptModelOutcome(
        outcome_id=outcome_id,
        severity=severity,
        remediation=remediation,
        parameters=_outcome_parameters(parameters),
        provider_detail=detail,
    )


_PROFILE_V1_KEYS = frozenset(
    {
        "profile_id",
        "family",
        "endpoint",
        "model_id",
        "model_digest",
        "adapter_version",
        "parser_version",
        "license_id",
        "license_source",
        "license_text_sha256",
        "capabilities",
    }
)
_PROFILE_V2_KEYS = frozenset(
    {
        *_PROFILE_V1_KEYS,
        "provider_label",
        "wire_dialect",
        "discovery_routes",
        "chat_route",
        "task_modes",
        "model_revision",
        "request_timeout_seconds",
        "max_retries",
        "max_concurrency",
        "max_calls_per_action",
        "cost_class",
        "usage_receipt_required",
        "retention_policy",
        "qualification_state",
        "limitations",
    }
)
_CATALOG_V1_ROOT_KEYS = frozenset({"schema", "profiles"})
_CATALOG_V2_ROOT_KEYS = frozenset({"schema", "default_profile_id", "profiles"})
_PROFILE_V3_KEYS = _PROFILE_V2_KEYS | {"qualification_evidence"}
_CATALOG_V3_ROOT_KEYS = _CATALOG_V2_ROOT_KEYS
_LOCAL_QUALIFICATION_EVIDENCE_V3_KEYS = frozenset(
    {
        "model_id",
        "model_digest",
        "model_size_bytes",
        "model_format",
        "model_family",
        "parameter_size",
        "quantization_level",
        "context_length",
        "required_capabilities",
        "license_text_sha256",
        "show_identity_sha256",
        "adapter_version",
        "parser_version",
        "evidence_basis_id",
    }
)
_LOCAL_QUALIFICATION_EVIDENCE_V4_KEYS = _LOCAL_QUALIFICATION_EVIDENCE_V3_KEYS | {"kind"}
_REMOTE_QUALIFICATION_EVIDENCE_KEYS = frozenset(
    {
        "kind",
        "provider_id",
        "profile_id",
        "model_id",
        "adapter_version",
        "parser_version",
        "policy_version",
        "policy_sha256",
        "price_basis_id",
        "qualified_on",
        "source_checked_on",
        "price_valid_through",
        "max_transmissions",
        "observed_transmissions",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_micro_usd",
        "prompt_tokens",
        "completion_tokens",
        "actual_cost_micro_usd",
        "qualification_sha256",
        "repository_commit",
        "repository_tree",
    }
)
#: M22-18 added `basis` to both wire forms. The key sets are compared with exact equality, so the
#: new member is a versioned set that accepts either, exactly as `_LOCAL_QUALIFICATION_EVIDENCE_V4`
#: does for `kind`. An artifact or catalog entry without the key predates the reachability basis and
#: can only ever have described a completion, which is what absence is read as.
_REMOTE_QUALIFICATION_EVIDENCE_V2_KEYS = _REMOTE_QUALIFICATION_EVIDENCE_KEYS | {"basis"}
_REMOTE_QUALIFICATION_RESULT_KEYS = frozenset(
    {
        "schema",
        "mode",
        "status",
        "provider_id",
        "profile_id",
        "model_id",
        "adapter_version",
        "parser_version",
        "policy_version",
        "policy_sha256",
        "price_basis_id",
        "price_checked_on",
        "price_valid_through",
        "observed_on",
        "repository_commit",
        "repository_tree",
        "max_transmissions",
        "observed_transmissions",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_micro_usd",
        "outcome_id",
        "schema_valid",
        "receipt_valid",
        "receipt",
        "catalog_promotion_performed",
    }
)
_REMOTE_QUALIFICATION_RESULT_V2_KEYS = _REMOTE_QUALIFICATION_RESULT_KEYS | {"basis"}
#: Token counters are zero when usage is absent or the provider returned quota refusal.
#: Request/response byte counts independently prove the content route was reached.
_ZEROABLE = frozenset({"prompt_tokens", "completion_tokens"})
_REMOTE_QUALIFICATION_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "profile_id",
        "outcome_id",
        "http_status",
        "request_bytes",
        "response_bytes",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "provider_id",
        "model_id",
        "policy_sha256",
        "price_basis_id",
        "maximum_cost_micro_usd",
        "actual_cost_micro_usd",
        "usage_present",
    }
)
_TASK_MODES = frozenset({"t2va", "i2va", "fl2va", "l2va", "ref2va"})
_DIALECT_ROUTES: Mapping[PromptModelDialect, tuple[frozenset[str], frozenset[str]]] = {
    PromptModelDialect.OLLAMA_CHAT: (
        frozenset({"/api/tags", "/api/show"}),
        frozenset({"/api/chat"}),
    ),
    PromptModelDialect.OPENAI_CHAT_COMPLETIONS: (
        frozenset({"/v1/models", "/v1beta/openai/models"}),
        frozenset({"/v1/chat/completions", "/v1beta/openai/chat/completions"}),
    ),
    PromptModelDialect.ANTHROPIC_MESSAGES: (
        frozenset({"/v1/models"}),
        frozenset({"/v1/messages"}),
    ),
}


_BARE_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_SHOW_FACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_]{0,31}")
_GIT_OID = re.compile(r"[0-9a-f]{40}")
MAX_QUALIFICATION_CAPABILITIES = 16
MIN_QUALIFIED_CONTEXT_LENGTH = 8_192
MAX_QUALIFIED_CONTEXT_LENGTH = 1 << 24
MAX_QUALIFIED_MODEL_BYTES = 1 << 41


def normalize_ollama_digest(value: object) -> str:
    """Convert one bare lowercase Ollama tags digest into the repository digest domain.

    Ollama reports `digest` as bare lowercase hex; this repository spells every digest
    `sha256:<64 lowercase hex>`. The conversion happens here, exactly once, and is deliberately
    total in its refusals: an already-prefixed value, uppercase, a shortened prefix, surrounding
    whitespace or any non-hex character is refused rather than repaired. A digest that has been
    "helpfully" normalised is a digest nobody compared.
    """

    if type(value) is not str or _BARE_SHA256.fullmatch(value) is None:
        _fail("qualification_digest")
    return f"sha256:{value}"


def compute_show_identity_fingerprint(
    *,
    model_id: str,
    format_id: str,
    family: str,
    parameter_size: str,
    quantization_level: str,
    capabilities: Sequence[str],
    license_text_sha256: str,
) -> str:
    """Fingerprint the allowlisted show facts, and only those.

    Raw license text, the template, sampling parameters, tensor tables and any other provider prose
    are excluded by construction: this function cannot see them. The material is joined with a
    separator that none of the fields may contain, so two different fact sets cannot collide by
    concatenation.
    """

    from hashlib import sha256

    material = "\n".join(
        (
            model_id,
            format_id,
            family,
            parameter_size,
            quantization_level,
            ",".join(capabilities),
            license_text_sha256,
        )
    )
    return "sha256:" + sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptModelQualificationEvidence:
    """What an executed local qualification observed, reduced to allowlisted facts.

    Every field is either an identity a later run can re-observe and compare, or a fingerprint of
    material that must never be stored raw. There is no free-text field and no `to_wire` escape for
    provider prose, so this object cannot become a channel for model output, license text or a
    local path.
    """

    model_id: str
    model_digest: str
    model_size_bytes: int
    model_format: str
    model_family: str
    parameter_size: str
    quantization_level: str
    context_length: int
    required_capabilities: tuple[str, ...]
    license_text_sha256: str
    show_identity_sha256: str
    adapter_version: str
    parser_version: str
    evidence_basis_id: str

    def __post_init__(self) -> None:
        if _MODEL_ID.fullmatch(self.model_id) is None:
            _fail("qualification_model_id")
        if _SHA256.fullmatch(self.model_digest) is None:
            _fail("qualification_digest")
        for field_name in ("model_format", "model_family", "parameter_size", "quantization_level"):
            if _SHOW_FACT.fullmatch(getattr(self, field_name)) is None:
                _fail("qualification_show_fact")
        if (
            type(self.model_size_bytes) is not int
            or isinstance(self.model_size_bytes, bool)
            or not 0 < self.model_size_bytes <= MAX_QUALIFIED_MODEL_BYTES
        ):
            _fail("qualification_size")
        if (
            type(self.context_length) is not int
            or isinstance(self.context_length, bool)
            or not MIN_QUALIFIED_CONTEXT_LENGTH
            <= self.context_length
            <= MAX_QUALIFIED_CONTEXT_LENGTH
        ):
            _fail("qualification_context_length")
        capabilities = self.required_capabilities
        if (
            type(capabilities) is not tuple
            or not capabilities
            or len(capabilities) > MAX_QUALIFICATION_CAPABILITIES
        ):
            _fail("qualification_capabilities")
        if any(_CAPABILITY.fullmatch(item) is None for item in capabilities):
            _fail("qualification_capabilities")
        if list(capabilities) != sorted(capabilities) or len(set(capabilities)) != len(
            capabilities
        ):
            _fail("qualification_capabilities")
        # Generation is the only capability this item authorises. A model may additionally declare
        # vision, tools or thinking -- the qwen3.8 row declares all three -- and those are recorded
        # rather than hidden, because the request/response guards that refuse them have to know they
        # are possible. What is refused is a model that cannot complete at all.
        if "completion" not in capabilities:
            _fail("qualification_capabilities")
        # Both fingerprints live in the repository digest domain, `sha256:<64 lowercase hex>`,
        # the same spelling the catalog's own `model_digest`/`license_text_sha256` fields use. Bare
        # hex appears in exactly one place -- the Ollama tags wire -- and `normalize_ollama_digest`
        # is the single crossing point.
        for field_name in ("license_text_sha256", "show_identity_sha256"):
            if _SHA256.fullmatch(getattr(self, field_name)) is None:
                _fail("qualification_fingerprint")
        for field_name in ("adapter_version", "parser_version", "evidence_basis_id"):
            if _SAFE_VERSION.fullmatch(getattr(self, field_name)) is None:
                _fail("qualification_version")
        expected = compute_show_identity_fingerprint(
            model_id=self.model_id,
            format_id=self.model_format,
            family=self.model_family,
            parameter_size=self.parameter_size,
            quantization_level=self.quantization_level,
            capabilities=capabilities,
            license_text_sha256=self.license_text_sha256,
        )
        if self.show_identity_sha256 != expected:
            _fail("qualification_identity")

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": "local_ollama",
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "model_size_bytes": self.model_size_bytes,
            "model_format": self.model_format,
            "model_family": self.model_family,
            "parameter_size": self.parameter_size,
            "quantization_level": self.quantization_level,
            "context_length": self.context_length,
            "required_capabilities": list(self.required_capabilities),
            "license_text_sha256": self.license_text_sha256,
            "show_identity_sha256": self.show_identity_sha256,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "evidence_basis_id": self.evidence_basis_id,
        }


def _qualification_date(value: object, code: str) -> date:
    if type(value) is not str:
        _fail(code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail(code)
    if parsed.isoformat() != value:
        _fail(code)
    return parsed


@dataclass(frozen=True, slots=True)
class RemotePromptModelQualificationEvidence:
    """A reduced claim derived only from an authorized, bounded live qualification PASS.

    Remote aliases publish no weight digest. This evidence therefore binds the exact curated
    policy, observed alias, executed bounds and immutable qualification artifact instead of
    fabricating a local-style digest. Its wire form is an allowlist: no prompt, response, host,
    credential, request identifier, account or provider prose can enter the catalog through it.
    """

    provider_id: str
    profile_id: str
    model_id: str
    policy_version: str
    policy_sha256: str
    price_basis_id: str
    qualified_on: str
    source_checked_on: str
    price_valid_through: str
    max_transmissions: int
    observed_transmissions: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micro_usd: int
    prompt_tokens: int
    completion_tokens: int
    actual_cost_micro_usd: int
    qualification_sha256: str
    repository_commit: str
    repository_tree: str
    adapter_version: str
    parser_version: str
    family: PromptModelFamily = PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
    basis: RemoteQualificationBasis = RemoteQualificationBasis.COMPLETION

    def __post_init__(self) -> None:
        if self.family not in {
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            PromptModelFamily.REMOTE_ANTHROPIC,
        }:
            _fail("remote_qualification_family")
        for field_name in ("provider_id", "profile_id", "price_basis_id"):
            if _PROFILE_ID.fullmatch(getattr(self, field_name)) is None:
                _fail("remote_qualification_identity")
        if _MODEL_ID.fullmatch(self.model_id) is None:
            _fail("remote_qualification_identity")
        for field_name in ("policy_version", "adapter_version", "parser_version"):
            if _SAFE_VERSION.fullmatch(getattr(self, field_name)) is None:
                _fail("remote_qualification_version")
        for field_name in ("policy_sha256", "qualification_sha256"):
            if _SHA256.fullmatch(getattr(self, field_name)) is None:
                _fail("remote_qualification_fingerprint")
        for field_name in ("repository_commit", "repository_tree"):
            if _GIT_OID.fullmatch(getattr(self, field_name)) is None:
                _fail("remote_qualification_repository")
        # IMPORTANT: legacy price dates are metadata, never a qualification expiry window.
        # Ordering the observation inside that window restores the removed billing blocker.
        for date_value in (self.source_checked_on, self.qualified_on, self.price_valid_through):
            _qualification_date(date_value, "remote_qualification_date")
        if (
            type(self.max_transmissions) is not int
            or self.max_transmissions not in {2, 4}
            or type(self.observed_transmissions) is not int
            or not 2 <= self.observed_transmissions <= self.max_transmissions
        ):
            _fail("remote_qualification_transmissions")
        for value in (self.max_input_tokens, self.max_output_tokens):
            if type(value) is not int or not 1 <= value <= MAX_TOKEN_CEILING:
                _fail("remote_qualification_tokens")
        if not isinstance(self.basis, RemoteQualificationBasis):
            _fail("remote_qualification_basis")
        completion = self.basis is RemoteQualificationBasis.COMPLETION
        for value in (self.prompt_tokens, self.completion_tokens):
            # Optional observed usage has no relationship to the old billing-token caps.
            # A quota basis still cannot claim a generated completion or its usage.
            if type(value) is not int or (
                not 0 <= value <= MAX_TOKEN_CEILING if completion else value != 0
            ):
                _fail("remote_qualification_tokens")
        for value in (self.max_cost_micro_usd, self.actual_cost_micro_usd):
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                _fail("remote_qualification_cost")

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": (
                "remote_anthropic"
                if self.family is PromptModelFamily.REMOTE_ANTHROPIC
                else "remote_openai_compatible"
            ),
            "provider_id": self.provider_id,
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "price_basis_id": self.price_basis_id,
            "qualified_on": self.qualified_on,
            "source_checked_on": self.source_checked_on,
            "price_valid_through": self.price_valid_through,
            "max_transmissions": self.max_transmissions,
            "observed_transmissions": self.observed_transmissions,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_micro_usd": self.max_cost_micro_usd,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "actual_cost_micro_usd": self.actual_cost_micro_usd,
            "qualification_sha256": self.qualification_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "basis": self.basis.value,
        }


def build_remote_qualification_evidence(
    values: object,
    *,
    qualification_sha256: object,
    family: PromptModelFamily = PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
) -> RemotePromptModelQualificationEvidence:
    """Reduce one exact live PASS artifact to the only fields a catalog may retain."""

    if not isinstance(values, Mapping) or set(values) not in (
        _REMOTE_QUALIFICATION_RESULT_KEYS,
        _REMOTE_QUALIFICATION_RESULT_V2_KEYS,
    ):
        _fail("remote_qualification_keys")
    if family not in {
        PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        PromptModelFamily.REMOTE_ANTHROPIC,
    }:
        _fail("remote_qualification_family")
    # SECURITY: the basis is derived from what the service actually answered, never taken from the
    # artifact's own label. The two admissible outcomes are disjoint, so a completion that failed a
    # later check cannot fall back to the weaker claim; and an artifact naming a basis its own data
    # does not support is refused rather than believed. Every other outcome is excluded on purpose:
    # AUTHENTICATION and MODEL_MISSING prove nothing about this profile being usable, and the rest
    # say this repository built a request the model refused, which must stay visible as our defect.
    basis_by_outcome = {
        PromptModelOutcomeId.OK.value: RemoteQualificationBasis.COMPLETION,
        PromptModelOutcomeId.QUOTA.value: RemoteQualificationBasis.REACHABILITY,
    }
    basis = basis_by_outcome.get(values["outcome_id"])
    if basis is None:
        _fail("remote_qualification_basis")
    # An artifact without the key predates the reachability basis and could only ever have
    # described a completion, which is how the decoder reads absence and how it is read here. A
    # quota-shaped result may not obtain the weaker classification by omitting the label: the
    # basis is something an artifact asserts and the data confirms, never something it inherits
    # from being old. The two versioned key sets are symmetric on this or neither is trustworthy.
    declared = values.get("basis", RemoteQualificationBasis.COMPLETION.value)
    if declared != basis.value:
        _fail("remote_qualification_basis")
    completion = basis is RemoteQualificationBasis.COMPLETION
    if (
        values["schema"] != REMOTE_QUALIFICATION_RESULT_SCHEMA
        or values["mode"] != "live"
        or values["status"] != "PASS"
        # Both directions, so the artifact must be internally consistent: a completion carries a
        # validated draft and receipt, and a quota run cannot claim to have produced either.
        or values["schema_valid"] is not completion
        or values["receipt_valid"] is not completion
        or values["catalog_promotion_performed"] is not False
    ):
        _fail("remote_qualification_result")
    for key in (
        "adapter_version",
        "parser_version",
        "policy_version",
        "price_checked_on",
        "price_valid_through",
        "observed_on",
        "repository_commit",
        "repository_tree",
    ):
        if type(values[key]) is not str:
            _fail("remote_qualification_field")
    for key in (
        "max_transmissions",
        "observed_transmissions",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_micro_usd",
    ):
        if type(values[key]) is not int:
            _fail("remote_qualification_field")
    receipt = values["receipt"]
    if not isinstance(receipt, Mapping) or set(receipt) != _REMOTE_QUALIFICATION_RECEIPT_KEYS:
        _fail("remote_qualification_receipt")
    if (
        receipt["schema"] != REMOTE_QUALIFICATION_RECEIPT_SCHEMA
        # Stronger than the literal it replaces: the receipt must agree with the result it belongs
        # to, so a completion receipt can never be attached to a quota result or the reverse.
        or receipt["outcome_id"] != values["outcome_id"]
        or receipt["http_status"] != (200 if completion else 429)
        or type(receipt["usage_present"]) is not bool
        or (not completion and receipt["usage_present"])
    ):
        _fail("remote_qualification_receipt")
    for key in ("profile_id", "provider_id", "model_id", "policy_sha256"):
        if type(values[key]) is not str or receipt[key] != values[key]:
            _fail("remote_qualification_receipt")
    if type(receipt["price_basis_id"]) is not str or len(receipt["price_basis_id"]) > 128:
        _fail("remote_qualification_receipt")
    for key in (
        "request_bytes",
        "response_bytes",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "maximum_cost_micro_usd",
        "actual_cost_micro_usd",
    ):
        # SECURITY: `request_bytes` and `response_bytes` keep their floor on both bases, and that
        # is the entire reach proof -- a content request was serialized and sent, and the service
        # answered it. The retained M22-14 Gemini artifact carries `request_bytes: 0` because it
        # never got past discovery, and it can therefore qualify on neither basis.
        if key in _ZEROABLE and (not completion or not receipt["usage_present"]):
            # Absent usage has zero token counters; this says nothing about provider charges.
            if type(receipt[key]) is not int or receipt[key] != 0:
                _fail("remote_qualification_receipt")
            continue
        minimum = 1 if key in {"request_bytes", "response_bytes"} else 0
        if type(receipt[key]) is not int or receipt[key] < minimum:
            _fail("remote_qualification_receipt")
    # IMPORTANT: current product receipts deliberately carry empty/zero billing facts.
    # Qualification binds transport and policy identity, not a charge or maintainer fee ceiling.
    if type(qualification_sha256) is not str:
        _fail("remote_qualification_fingerprint")
    return RemotePromptModelQualificationEvidence(
        provider_id=values["provider_id"],
        profile_id=values["profile_id"],
        model_id=values["model_id"],
        policy_version=values["policy_version"],
        policy_sha256=values["policy_sha256"],
        price_basis_id=values["price_basis_id"],
        qualified_on=values["observed_on"],
        source_checked_on=values["price_checked_on"],
        price_valid_through=values["price_valid_through"],
        max_transmissions=values["max_transmissions"],
        observed_transmissions=values["observed_transmissions"],
        max_input_tokens=values["max_input_tokens"],
        max_output_tokens=values["max_output_tokens"],
        max_cost_micro_usd=values["max_cost_micro_usd"],
        prompt_tokens=receipt["prompt_tokens"],
        completion_tokens=receipt["completion_tokens"],
        actual_cost_micro_usd=receipt["actual_cost_micro_usd"],
        qualification_sha256=qualification_sha256,
        repository_commit=values["repository_commit"],
        repository_tree=values["repository_tree"],
        adapter_version=values["adapter_version"],
        parser_version=values["parser_version"],
        family=family,
        basis=basis,
    )


def build_qualification_evidence(
    *,
    model_id: str,
    digest: str,
    model_size_bytes: int,
    model_format: str,
    model_family: str,
    parameter_size: str,
    quantization_level: str,
    context_length: int,
    capabilities: Sequence[str],
    license_text_sha256: str,
    adapter_version: str,
    parser_version: str,
    evidence_basis_id: str,
) -> PromptModelQualificationEvidence:
    """Build evidence, deriving the identity fingerprint rather than accepting one.

    Every producer -- the qualification tool, the catalog generator and the tests -- goes through
    this function, so the fingerprint can never disagree with the facts it is supposed to summarise.
    `digest` accepts either the bare Ollama spelling or the repository domain; the conversion is
    delegated to `normalize_ollama_digest` so it still happens in exactly one place.
    """

    normalized_digest = digest if _SHA256.fullmatch(digest) else normalize_ollama_digest(digest)
    capability_tuple = tuple(sorted(capabilities))
    return PromptModelQualificationEvidence(
        model_id=model_id,
        model_digest=normalized_digest,
        model_size_bytes=model_size_bytes,
        model_format=model_format,
        model_family=model_family,
        parameter_size=parameter_size,
        quantization_level=quantization_level,
        context_length=context_length,
        required_capabilities=capability_tuple,
        license_text_sha256=license_text_sha256,
        show_identity_sha256=compute_show_identity_fingerprint(
            model_id=model_id,
            format_id=model_format,
            family=model_family,
            parameter_size=parameter_size,
            quantization_level=quantization_level,
            capabilities=capability_tuple,
            license_text_sha256=license_text_sha256,
        ),
        adapter_version=adapter_version,
        parser_version=parser_version,
        evidence_basis_id=evidence_basis_id,
    )


MAX_LICENSE_TEXT_CHARACTERS = 1 << 20
#: The ingest ceiling for one untrusted provider model listing, shared by every reader of one.
#: It lives in this module because it is the lowest one that needs it: `prompt_model_session`
#: imports from here and never the reverse, so a single definition costs no import cycle.
#: M22-19 raised it from 64, which was sized for a local Ollama tag list and refused a hosted model
#: catalogue outright: the M22-14 Gemini discovery response was 8339 bytes, on the order of a
#: hundred and fifty rows. The response itself is already capped at MAX_PROMPT_MODEL_RESPONSE_BYTES,
#: so this is the cheaper second guard behind that one and does not need to be tight.
#: SECURITY: one listing gets one ceiling, and every reader on the readiness path consults this
#: name. Two ceilings on one listing do not compose -- the lower one decides and the other is dead
#: until someone raises it, which is how a hosted catalogue and then a large local tag list each
#: became a silent MALFORMED_RESPONSE. Do not reintroduce a private bound beside this one.
MAX_DISCOVERY_ROWS = 512


def compute_endpoint_fingerprint(endpoint: object) -> str:
    """Fingerprint an endpoint so readiness evidence can be bound to it without storing it.

    Evidence has to be able to say "this readiness result belongs to the endpoint you are about to
    call" while never carrying the endpoint itself, because an endpoint is exactly the kind of
    local detail the receipt rules exclude. A fingerprint answers the only question asked of it --
    same or not the same -- and answers nothing else.
    """

    from hashlib import sha256

    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_CHARACTERS:
        _fail("readiness_endpoint")
    return "sha256:" + sha256(endpoint.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExactTagsRow:
    """The one tags row that is allowed to be authority for one exact model."""

    model_id: str
    model_digest: str
    model_size_bytes: int


@dataclass(frozen=True, slots=True)
class ShowIdentity:
    """The allowlisted show facts, with the licence already reduced to a fingerprint."""

    model_format: str
    model_family: str
    parameter_size: str
    quantization_level: str
    context_length: int
    capabilities: tuple[str, ...]
    license_text_sha256: str


def parse_exact_tags_row(payload: object, model_id: object) -> ExactTagsRow:
    """Find the single tags row for one exact model, or refuse the whole listing.

    This is deliberately stricter than the best-effort census used for discovery. There, a listing
    is a menu and a row that cannot be read is a row the user does not get offered. Here the
    listing is being asked to prove which weights are loaded, so a malformed row, a duplicate of
    the selected id, or a row whose name and model fields disagree invalidates the answer instead
    of being skipped past: a partial reading of an untrusted listing must never become the basis
    for calling a model qualified.
    """

    if not isinstance(model_id, str) or _MODEL_ID.fullmatch(model_id) is None:
        _fail("readiness_tags_model_id")
    if not isinstance(payload, Mapping):
        _fail("readiness_tags")
    rows = payload.get("models")
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        _fail("readiness_tags")
    # SECURITY: bound the array before reading any row, so an oversized listing cannot be paid for
    # one row at a time. M22-19: this is the same listing `_census` and `resolve_live_identity`
    # read, so it is bounded by the same name -- a private ceiling here silently outranked the
    # census one for every Ollama profile with more pulled models than it allowed.
    if len(rows) > MAX_DISCOVERY_ROWS:
        _fail("readiness_tags")
    found: ExactTagsRow | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("readiness_tags")
        name = row.get("name")
        identifier = row.get("model")
        if not isinstance(name, str) or not isinstance(identifier, str):
            _fail("readiness_tags")
        if name != identifier:
            _fail("readiness_tags")
        if name != model_id:
            continue
        if any(row.get(key) not in (None, "") for key in ("remote_model", "remote_host")):
            # SECURITY: loopback is a destination fact, not proof of local model execution.
            # Reject advertised cloud routing before reducing native tag data to an identity.
            _fail("readiness_cloud_route")
        if found is not None:
            # Two rows claiming the same exact id: there is no way to tell which would answer.
            _fail("readiness_tags_ambiguous")
        size = row.get("size")
        if (
            type(size) is not int
            or isinstance(size, bool)
            or not 0 < size <= MAX_QUALIFIED_MODEL_BYTES
        ):
            _fail("readiness_tags_size")
        if not isinstance(row.get("details"), Mapping):
            _fail("readiness_tags_details")
        found = ExactTagsRow(
            model_id=model_id,
            model_digest=normalize_ollama_digest(row.get("digest")),
            model_size_bytes=size,
        )
    if found is None:
        _fail("readiness_tags_absent")
    return found


def _show_context_length(model_info: object, family: str) -> int:
    """Read the family's own context-length key out of the model_info table.

    Ollama namespaces these by architecture (qwen35.context_length), so the key is derived from the
    family that was already validated as a show fact rather than searched for by suffix. A suffix
    search would let an unrelated architecture's key answer for this one.
    """

    if not isinstance(model_info, Mapping):
        _fail("readiness_show_context_length")
    value = model_info.get(family + ".context_length")
    if (
        type(value) is not int
        or isinstance(value, bool)
        or not MIN_QUALIFIED_CONTEXT_LENGTH <= value <= MAX_QUALIFIED_CONTEXT_LENGTH
    ):
        _fail("readiness_show_context_length")
    return value


def parse_show_identity(payload: object) -> ShowIdentity:
    """Reduce one show response to allowlisted facts and one licence fingerprint.

    The raw licence, the prompt template, the sampling parameters and the tensor table are read and
    dropped here; only the licence's fingerprint survives the function. Nothing this returns can
    carry provider prose onward, because none of the fields it returns is free text.
    """

    if not isinstance(payload, Mapping):
        _fail("readiness_show")
    details = payload.get("details")
    if not isinstance(details, Mapping):
        _fail("readiness_show")
    facts: list[str] = []
    for key in ("format", "family", "parameter_size", "quantization_level"):
        value = details.get(key)
        if not isinstance(value, str) or _SHOW_FACT.fullmatch(value) is None:
            _fail("readiness_show_fact")
        facts.append(value)
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, Sequence) or isinstance(capabilities, str | bytes):
        _fail("readiness_show_capabilities")
    if not capabilities or len(capabilities) > MAX_QUALIFICATION_CAPABILITIES:
        _fail("readiness_show_capabilities")
    for item in capabilities:
        if not isinstance(item, str) or _CAPABILITY.fullmatch(item) is None:
            _fail("readiness_show_capabilities")
    ordered = tuple(sorted(capabilities))
    if len(set(ordered)) != len(ordered):
        _fail("readiness_show_capabilities")
    context_length = _show_context_length(payload.get("model_info"), facts[1])
    license_text = payload.get("license")
    if not isinstance(license_text, str) or not license_text:
        _fail("readiness_show_license")
    if len(license_text) > MAX_LICENSE_TEXT_CHARACTERS:
        _fail("readiness_show_license")
    from hashlib import sha256

    return ShowIdentity(
        model_format=facts[0],
        model_family=facts[1],
        parameter_size=facts[2],
        quantization_level=facts[3],
        context_length=context_length,
        capabilities=ordered,
        license_text_sha256="sha256:" + sha256(license_text.encode("utf-8")).hexdigest(),
    )


def match_qualification_identity(
    evidence: object, tags: object, show: object
) -> PromptModelOutcomeId | None:
    """Compare what is running now against what was qualified. None means they agree.

    Every disagreement gets its own closed outcome rather than one generic mismatch, because the
    remediations genuinely differ: a replaced digest is a re-pull, a shrunken context window is a
    server configuration, and a changed licence fingerprint is a model the operator must re-review.
    """

    if not isinstance(evidence, PromptModelQualificationEvidence):
        _fail("readiness_match")
    if not isinstance(tags, ExactTagsRow) or not isinstance(show, ShowIdentity):
        _fail("readiness_match")
    if tags.model_id != evidence.model_id:
        return PromptModelOutcomeId.MODEL_MISSING
    if tags.model_digest != evidence.model_digest:
        return PromptModelOutcomeId.DIGEST_MISMATCH
    if tags.model_size_bytes != evidence.model_size_bytes:
        return PromptModelOutcomeId.DIGEST_MISMATCH
    if "completion" not in show.capabilities:
        return PromptModelOutcomeId.CAPABILITY_MISMATCH
    if show.context_length < evidence.context_length:
        # A smaller window than the one qualified is a different machine's answer to the same
        # question. Larger is accepted: the qualified floor is still satisfied.
        return PromptModelOutcomeId.CAPABILITY_MISMATCH
    if show.license_text_sha256 != evidence.license_text_sha256:
        return PromptModelOutcomeId.CAPABILITY_MISMATCH
    observed = compute_show_identity_fingerprint(
        model_id=evidence.model_id,
        format_id=show.model_format,
        family=show.model_family,
        parameter_size=show.parameter_size,
        quantization_level=show.quantization_level,
        capabilities=show.capabilities,
        license_text_sha256=show.license_text_sha256,
    )
    if observed != evidence.show_identity_sha256:
        return PromptModelOutcomeId.CAPABILITY_MISMATCH
    return None


@dataclass(frozen=True, slots=True)
class QualifiedIdentityObservation:
    """What a probe saw: one exact qualified model, at one endpoint, at one moment.

    This is deliberately *not* the readiness evidence. A probe owns a socket and can honestly say
    what answered it; it does not own the session authority counters and must not be able to mint
    a value that claims them. Stamping is a separate step, performed by the only object that knows
    those counters -- see `stamp_readiness_evidence`.
    """

    profile_id: str
    model_id: str
    model_digest: str | None
    qualification_sha256: str
    endpoint_sha256: str
    observed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or _PROFILE_ID.fullmatch(self.profile_id) is None:
            _fail("readiness_evidence_profile")
        if _MODEL_ID.fullmatch(self.model_id) is None:
            _fail("readiness_evidence_model")
        if self.model_digest is not None and _SHA256.fullmatch(self.model_digest) is None:
            _fail("readiness_evidence_fingerprint")
        for field_name in ("qualification_sha256", "endpoint_sha256"):
            if _SHA256.fullmatch(getattr(self, field_name)) is None:
                _fail("readiness_evidence_fingerprint")
        observed_at = self.observed_at
        if (
            type(observed_at) is not float
            or observed_at != observed_at
            or observed_at in (float("inf"), float("-inf"))
            or observed_at < 0.0
        ):
            _fail("readiness_evidence_observed_at")


@dataclass(frozen=True, slots=True)
class ProviderReadinessEvidence:
    """That one exact qualified model was observed running under known authority.

    This is the object that lets a later generation call say "the thing I verified is the thing I
    am about to call". It holds fingerprints and identities only: no credential, no endpoint URL,
    no raw response and no provider prose, so it stays safe to keep in session state and to project
    to a browser. It is not a permission -- a caller still rechecks the exact supported identity
    basis before each chat -- it is the record of what the permission was granted against.
    """

    profile_id: str
    model_id: str
    model_digest: str | None
    qualification_sha256: str
    endpoint_sha256: str
    observed_at: float
    provider_revision: int
    authority_epoch: int

    def __post_init__(self) -> None:
        if _PROFILE_ID.fullmatch(self.profile_id) is None:
            _fail("readiness_evidence_profile")
        if _MODEL_ID.fullmatch(self.model_id) is None:
            _fail("readiness_evidence_model")
        if self.model_digest is not None and _SHA256.fullmatch(self.model_digest) is None:
            _fail("readiness_evidence_fingerprint")
        for field_name in ("qualification_sha256", "endpoint_sha256"):
            if _SHA256.fullmatch(getattr(self, field_name)) is None:
                _fail("readiness_evidence_fingerprint")
        observed_at = self.observed_at
        if (
            type(observed_at) is not float
            or observed_at != observed_at
            or observed_at in (float("inf"), float("-inf"))
            or observed_at < 0.0
        ):
            _fail("readiness_evidence_observed_at")
        # The same two counters `ProviderExecutionSnapshot` already uses as execution authority.
        # Binding readiness to them is what makes "release, expiry or an authority-epoch change
        # invalidates this" a comparison rather than a promise.
        for counter in (self.provider_revision, self.authority_epoch):
            if type(counter) is not int or not 1 <= counter <= 2_147_483_647:
                _fail("readiness_evidence_revision")

    def matches(
        self,
        profile: object,
        endpoint_sha256: object,
        provider_revision: object,
        authority_epoch: object,
        model: ModelChoice | None = None,
    ) -> bool:
        """Whether this evidence still describes the authority about to be exercised.

        Every component is compared, none is defaulted, and an unexpected type answers False rather
        than raising: a caller asking "may I reuse this?" is entitled to a No, and a No is always
        the safe answer to give it.
        """

        if not isinstance(profile, PromptModelProfile) or not isinstance(endpoint_sha256, str):
            return False
        qualification = profile.qualification_evidence
        if isinstance(qualification, PromptModelQualificationEvidence):
            current_qualification_sha256 = qualification.show_identity_sha256
        elif isinstance(
            qualification, RemotePromptModelQualificationEvidence | ConnectionQualificationEvidence
        ):
            current_qualification_sha256 = qualification.qualification_sha256
        else:
            return False
        if isinstance(profile, LegacyPromptModelProfile):
            expected_model = profile.model_id
            digest_matches = profile.model_digest == self.model_digest
        elif model is not None and model.profile_id == profile.profile_id:
            expected_model = model.model_id
            digest_matches = (
                model.metadata is None or model.metadata.model_digest == self.model_digest
            )
        else:
            return False
        return (
            profile.profile_id == self.profile_id
            and expected_model == self.model_id
            and digest_matches
            and current_qualification_sha256 == self.qualification_sha256
            and endpoint_sha256 == self.endpoint_sha256
            and provider_revision == self.provider_revision
            and authority_epoch == self.authority_epoch
        )

    def to_wire(self) -> dict[str, object]:
        # Deliberately narrower than the field set: a surface needs to know *that* a qualified
        # identity was observed and when, never which fingerprints proved it.
        return {
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "observed_at": self.observed_at,
            "provider_revision": self.provider_revision,
            "authority_epoch": self.authority_epoch,
        }


def stamp_readiness_evidence(
    identity: object, *, provider_revision: object, authority_epoch: object
) -> ProviderReadinessEvidence:
    """Bind one observed identity to the authority counters in force when it was observed."""

    if not isinstance(identity, QualifiedIdentityObservation):
        _fail("readiness_evidence_identity")
    return ProviderReadinessEvidence(
        profile_id=identity.profile_id,
        model_id=identity.model_id,
        model_digest=identity.model_digest,
        qualification_sha256=identity.qualification_sha256,
        endpoint_sha256=identity.endpoint_sha256,
        observed_at=identity.observed_at,
        provider_revision=provider_revision,  # type: ignore[arg-type]
        authority_epoch=authority_epoch,  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class ConnectionQualificationEvidence:
    """A recorded wire observation qualifies a connection, never all of its listed models."""

    profile_id: str
    family: PromptModelFamily
    observation_model_id: str
    adapter_version: str
    parser_version: str
    observed_on: str
    evidence_basis_sha256: str
    max_transmissions: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or _PROFILE_ID.fullmatch(self.profile_id) is None:
            _fail("connection_evidence_profile")
        if not isinstance(self.family, PromptModelFamily):
            _fail("connection_evidence_family")
        if (
            not isinstance(self.observation_model_id, str)
            or _MODEL_ID.fullmatch(self.observation_model_id) is None
            or ".." in self.observation_model_id
        ):
            _fail("connection_evidence_model")
        for value in (self.adapter_version, self.parser_version):
            if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
                _fail("connection_evidence_version")
        _qualification_date(self.observed_on, "connection_evidence_date")
        if (
            not isinstance(self.evidence_basis_sha256, str)
            or _SHA256.fullmatch(self.evidence_basis_sha256) is None
        ):
            _fail("connection_evidence_fingerprint")
        if type(self.max_transmissions) is not int or not 1 <= self.max_transmissions <= 4:
            _fail("connection_evidence_transmissions")

    @property
    def qualification_sha256(self) -> str:
        material = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + sha256(material.encode("utf-8")).hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": "provider_connection",
            "profile_id": self.profile_id,
            "family": self.family.value,
            "observation_model_id": self.observation_model_id,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "observed_on": self.observed_on,
            "evidence_basis_sha256": self.evidence_basis_sha256,
            "max_transmissions": self.max_transmissions,
        }


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Bounded native model facts; unknown facts remain absent rather than inferred from names."""

    model_digest: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    capabilities: tuple[str, ...] = ()
    locality: str = "unknown"
    display_name: str | None = None
    created: int | None = None
    max_input_tokens: int | None = None
    structured_output: bool | None = None
    reasoning_mandatory: bool | None = None
    reasoning_control_supported: bool | None = None
    shutdown_date: str | None = None
    moving_alias: bool | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    license_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.model_digest is not None and (
            not isinstance(self.model_digest, str) or _SHA256.fullmatch(self.model_digest) is None
        ):
            _fail("model_metadata_digest")
        for count in (self.context_length, self.max_input_tokens, self.max_output_tokens):
            if count is not None and (
                type(count) is not int or not 1 <= count <= MAX_TOKEN_CEILING
            ):
                _fail("model_metadata_tokens")
        for value in (
            self.structured_output,
            self.reasoning_mandatory,
            self.reasoning_control_supported,
            self.moving_alias,
        ):
            if value is not None and type(value) is not bool:
                _fail("model_metadata_boolean")
        for text_value, maximum in (
            (self.display_name, 128),
            (self.family, 64),
            (self.parameter_size, 64),
            (self.quantization, 64),
        ):
            if text_value is not None and (
                not isinstance(text_value, str)
                or not 1 <= len(text_value) <= maximum
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text_value)
                or _BEARER.search(text_value)
                or _API_KEY.search(text_value)
            ):
                _fail("model_metadata_text")
        if self.created is not None and (
            type(self.created) is not int or not 0 <= self.created <= 253402300799
        ):
            _fail("model_metadata_created")
        if self.shutdown_date is not None:
            if not isinstance(self.shutdown_date, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", self.shutdown_date
            ):
                _fail("model_metadata_shutdown")
            try:
                date.fromisoformat(self.shutdown_date)
            except ValueError:
                _fail("model_metadata_shutdown")
        if self.license_sha256 is not None and (
            not isinstance(self.license_sha256, str)
            or _SHA256.fullmatch(self.license_sha256) is None
        ):
            _fail("model_metadata_license")
        if not isinstance(self.locality, str) or self.locality not in {
            "local",
            "cloud",
            "remote",
            "unknown",
        }:
            _fail("model_metadata_locality")
        if (
            type(self.capabilities) is not tuple
            or len(self.capabilities) > MAX_QUALIFICATION_CAPABILITIES
        ):
            _fail("model_metadata_capabilities")
        if any(
            not isinstance(value, str) or _CAPABILITY.fullmatch(value) is None
            for value in self.capabilities
        ):
            _fail("model_metadata_capabilities")
        if len(set(self.capabilities)) != len(self.capabilities):
            _fail("model_metadata_capabilities")

    def to_wire(self) -> dict[str, object]:
        return {
            "model_digest": self.model_digest,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
            "capabilities": list(self.capabilities),
            "locality": self.locality,
            "display_name": self.display_name,
            "created": self.created,
            "max_input_tokens": self.max_input_tokens,
            "structured_output": self.structured_output,
            "reasoning_mandatory": self.reasoning_mandatory,
            "reasoning_control_supported": self.reasoning_control_supported,
            "shutdown_date": self.shutdown_date,
            "moving_alias": self.moving_alias,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "license_sha256": self.license_sha256,
        }


def read_ollama_choice_metadata(payload: object, digest: str) -> ModelMetadata:
    """Read selected-only show facts without carrying licence/template or remote URLs onward."""
    if not isinstance(payload, Mapping):
        _fail("readiness_show")
    if any(payload.get(key) not in (None, "") for key in ("remote_model", "remote_host")):
        # SECURITY: native cloud routing must be refused before allowlist reduction discards it.
        _fail("readiness_cloud_route")
    raw_caps = payload.get("capabilities")
    if not isinstance(raw_caps, Sequence) or isinstance(raw_caps, str | bytes):
        _fail("readiness_show_capabilities")
    context = None
    family = None
    details = payload.get("details")
    info = payload.get("model_info")
    if (
        isinstance(details, Mapping)
        and isinstance(details.get("family"), str)
        and isinstance(info, Mapping)
    ):
        family = details["family"]
        key = family + ".context_length"
        if key in info:
            context = _show_context_length(info, family)
    thinking = payload.get("thinking")
    mandatory = None
    control = None
    if thinking is not None:
        if not isinstance(thinking, Mapping):
            _fail("readiness_show_thinking")
        values = thinking.get("values")
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 16
            or any(
                type(value) is not bool
                and (not isinstance(value, str) or _CAPABILITY.fullmatch(value) is None)
                for value in values
            )
        ):
            _fail("readiness_show_thinking")
        # IMPORTANT: named thinking levels do not authorize boolean false. Treat the exact
        # published controls as authority; guessing from a model name can enable reasoning.
        control = any(value is False for value in values)
        mandatory = not control
    licence = payload.get("license")
    if licence is not None and (
        not isinstance(licence, str)
        or len(licence) > MAX_CATALOG_BYTES
        or any(0xD800 <= ord(char) <= 0xDFFF for char in licence)
    ):
        _fail("readiness_show_license")
    return ModelMetadata(
        model_digest=digest,
        context_length=context,
        capabilities=tuple(raw_caps),
        locality="local",
        family=family,
        parameter_size=details.get("parameter_size") if isinstance(details, Mapping) else None,
        quantization=details.get("quantization_level") if isinstance(details, Mapping) else None,
        license_sha256="sha256:" + sha256(licence.encode()).hexdigest() if licence else None,
        reasoning_mandatory=mandatory,
        reasoning_control_supported=control,
    )


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """Exact server-created membership in one current connection census."""

    profile_id: str
    model_id: str
    listing_sha256: str
    listed_at_monotonic: float
    metadata: ModelMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or _PROFILE_ID.fullmatch(self.profile_id) is None:
            _fail("model_choice_profile")
        if (
            not isinstance(self.model_id, str)
            or _MODEL_ID.fullmatch(self.model_id) is None
            or ".." in self.model_id
        ):
            _fail("model_choice_model")
        if (
            not isinstance(self.listing_sha256, str)
            or _SHA256.fullmatch(self.listing_sha256) is None
        ):
            _fail("model_choice_listing")
        value = self.listed_at_monotonic
        if type(value) is not float or not 0.0 <= value < float("inf"):
            _fail("model_choice_time")
        if self.metadata is not None and not isinstance(self.metadata, ModelMetadata):
            _fail("model_choice_metadata")

    def to_wire(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "metadata": None if self.metadata is None else self.metadata.to_wire(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptModelProfile:
    """A package-owned provider connection with no chosen model or price authority."""

    profile_id: str
    family: PromptModelFamily
    endpoint: str
    adapter_version: str
    parser_version: str
    capabilities: PromptModelCapabilities
    provider_label: str = ""
    wire_dialect: PromptModelDialect = PromptModelDialect.LEGACY
    discovery_routes: tuple[str, ...] = ()
    chat_route: str = ""
    task_modes: tuple[str, ...] = ()
    request_timeout_seconds: int = 30
    max_retries: int = 0
    max_concurrency: int = 1
    max_calls_per_action: int = 2
    cost_class: str = "unclassified"
    usage_receipt_required: bool = False
    retention_policy: str = "unspecified"
    qualification_state: PromptModelQualificationState = (
        PromptModelQualificationState.LEGACY_UNQUALIFIED
    )
    qualification_evidence: (
        ConnectionQualificationEvidence
        | PromptModelQualificationEvidence
        | RemotePromptModelQualificationEvidence
        | None
    ) = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        evidence = self.qualification_evidence
        qualified = self.qualification_state is PromptModelQualificationState.QUALIFIED
        if qualified is (evidence is None):
            _fail("catalog_qualification")
        if evidence is not None and (
            not isinstance(evidence, ConnectionQualificationEvidence)
            or evidence.profile_id != self.profile_id
            or evidence.family is not self.family
            or evidence.adapter_version != self.adapter_version
            or evidence.parser_version != self.parser_version
            or evidence.max_transmissions != self.max_calls_per_action
        ):
            _fail("catalog_connection_qualification")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "family": self.family.value,
            "endpoint": self.endpoint,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "capabilities": self.capabilities.to_wire(),
            "provider_label": self.provider_label,
            "wire_dialect": self.wire_dialect.value,
            "discovery_routes": list(self.discovery_routes),
            "chat_route": self.chat_route,
            "task_modes": list(self.task_modes),
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "max_concurrency": self.max_concurrency,
            "max_calls_per_action": self.max_calls_per_action,
            "cost_class": self.cost_class,
            "usage_receipt_required": self.usage_receipt_required,
            "retention_policy": self.retention_policy,
            "qualification_state": self.qualification_state.value,
            "qualification_evidence": None
            if self.qualification_evidence is None
            else self.qualification_evidence.to_wire(),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyPromptModelProfile(PromptModelProfile):
    """Historical v1-v5 pinned rows; never loaded by the current catalogue."""

    model_id: str
    model_digest: str | None
    license_id: str
    license_source: str
    license_text_sha256: str | None
    model_revision: str | None = None

    def __post_init__(self) -> None:
        # The state and its evidence are one fact stated twice, so they are checked together here
        # rather than in each decoder. `qualified` without evidence would be an unfalsifiable claim;
        # evidence without `qualified` is a row that did the work and then failed to admit it.
        qualified = self.qualification_state is PromptModelQualificationState.QUALIFIED
        if qualified is (self.qualification_evidence is None):
            _fail("catalog_qualification")
        evidence = self.qualification_evidence
        if evidence is None:
            return
        if isinstance(evidence, ConnectionQualificationEvidence):
            _fail("catalog_qualification")
        if evidence.model_id != self.model_id:
            _fail("catalog_qualification_model")
        if isinstance(evidence, PromptModelQualificationEvidence):
            if self.family is not PromptModelFamily.OLLAMA:
                _fail("catalog_qualification_family")
            if self.model_digest != evidence.model_digest:
                _fail("catalog_qualification_digest")
            if self.license_text_sha256 != evidence.license_text_sha256:
                _fail("catalog_qualification_license")
        elif isinstance(evidence, RemotePromptModelQualificationEvidence):
            if (
                self.family
                not in {
                    PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
                    PromptModelFamily.REMOTE_ANTHROPIC,
                }
                or evidence.family is not self.family
                or self.model_digest is not None
                or evidence.profile_id != self.profile_id
            ):
                _fail("catalog_qualification_family")
        else:
            _fail("catalog_qualification")
        if evidence.adapter_version != self.adapter_version:
            _fail("catalog_qualification_version")
        if evidence.parser_version != self.parser_version:
            _fail("catalog_qualification_version")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "family": self.family.value,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "adapter_version": self.adapter_version,
            "parser_version": self.parser_version,
            "license_id": self.license_id,
            "license_source": self.license_source,
            "license_text_sha256": self.license_text_sha256,
            "capabilities": self.capabilities.to_wire(),
            "provider_label": self.provider_label,
            "wire_dialect": self.wire_dialect.value,
            "discovery_routes": list(self.discovery_routes),
            "chat_route": self.chat_route,
            "task_modes": list(self.task_modes),
            "model_revision": self.model_revision,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "max_concurrency": self.max_concurrency,
            "max_calls_per_action": self.max_calls_per_action,
            "cost_class": self.cost_class,
            "usage_receipt_required": self.usage_receipt_required,
            "retention_policy": self.retention_policy,
            "qualification_state": self.qualification_state.value,
            "qualification_evidence": (
                None
                if self.qualification_evidence is None
                else self.qualification_evidence.to_wire()
            ),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class PromptModelCatalog:
    """The admissible profiles. Being listed here is permission to be named, never to be chosen."""

    schema: str
    profiles: tuple[PromptModelProfile, ...]
    default_profile_id: None = None

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.profiles)

    def require(self, profile_id: object) -> PromptModelProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        _fail("unknown_profile")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            _fail("catalog_duplicate_key")
        values[key] = value
    return values


def _reject_constant(_: str) -> NoReturn:
    _fail("catalog_constant")


def _text(values: Mapping[str, object], key: str, pattern: re.Pattern[str], code: str) -> str:
    value = values[key]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(code)
    return value


def _bounded_text(values: Mapping[str, object], key: str, code: str, maximum: int = 128) -> str:
    value = values[key]
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(code)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(code)
    return value


def _decode_profile_v1(values: object, seen: set[str]) -> PromptModelProfile:
    if not isinstance(values, Mapping):
        _fail("catalog_profile_shape")
    keys = set(values)
    if keys != _PROFILE_V1_KEYS:
        _fail("catalog_profile_keys")

    profile_id = _text(values, "profile_id", _PROFILE_ID, "catalog_profile_id")
    if profile_id in seen:
        _fail("catalog_duplicate_profile")
    seen.add(profile_id)

    raw_family = values["family"]
    if not isinstance(raw_family, str) or raw_family not in {
        item.value for item in PromptModelFamily
    }:
        _fail("catalog_family")
    family = PromptModelFamily(raw_family)
    if family is PromptModelFamily.REMOTE_ANTHROPIC:
        _fail("catalog_family")

    capabilities = build_prompt_model_capabilities(values["capabilities"])
    if capabilities.family is not family:
        _fail("catalog_family")

    endpoint = values["endpoint"]
    if not isinstance(endpoint, str):
        _fail("catalog_endpoint")
    if family is PromptModelFamily.IN_PROCESS_GGUF:
        if endpoint:
            _fail("catalog_endpoint")
    else:
        admit_egress_destination(family, endpoint)

    return LegacyPromptModelProfile(
        profile_id=profile_id,
        family=family,
        endpoint=endpoint,
        model_id=_bounded_text(values, "model_id", "catalog_model_id"),
        model_digest=_text(values, "model_digest", _SHA256, "catalog_digest"),
        adapter_version=_text(values, "adapter_version", _SAFE_VERSION, "catalog_version"),
        parser_version=_text(values, "parser_version", _SAFE_VERSION, "catalog_version"),
        license_id=_bounded_text(values, "license_id", "catalog_license", 64),
        license_source=_bounded_text(values, "license_source", "catalog_license", 64),
        license_text_sha256=_text(values, "license_text_sha256", _SHA256, "catalog_digest"),
        capabilities=capabilities,
    )


def _optional_sha256(value: object, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _closed_text_tuple(
    value: object, *, code: str, maximum: int, allowed: frozenset[str] | None = None
) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        _fail(code)
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 128:
            _fail(code)
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in item):
            _fail(code)
        if allowed is not None and item not in allowed:
            _fail(code)
        if item in result:
            _fail(code)
        result.append(item)
    return tuple(result)


def _v2_bounded_int(values: Mapping[str, object], key: str, *, minimum: int, maximum: int) -> int:
    value = values[key]
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("catalog_bound")
    return value


def _decode_local_qualification_evidence(
    values: object, *, v4: bool
) -> PromptModelQualificationEvidence | None:
    """Decode the typed evidence object, or `None` for a row that never qualified.

    Every scalar is type-checked before the dataclass sees it, because the dataclass validates
    shape and self-consistency rather than JSON provenance: a bool is not an int here, a float is
    not a length, and an absent key is not an empty string.
    """

    if values is None:
        return None
    expected = (
        _LOCAL_QUALIFICATION_EVIDENCE_V4_KEYS if v4 else _LOCAL_QUALIFICATION_EVIDENCE_V3_KEYS
    )
    if not isinstance(values, Mapping) or set(values) != expected:
        _fail("qualification_keys")
    if v4 and values["kind"] != "local_ollama":
        _fail("qualification_kind")
    capabilities = values["required_capabilities"]
    if type(capabilities) is not list or any(type(item) is not str for item in capabilities):
        _fail("qualification_capabilities")
    for key in (
        "model_id",
        "model_digest",
        "model_format",
        "model_family",
        "parameter_size",
        "quantization_level",
        "license_text_sha256",
        "show_identity_sha256",
        "adapter_version",
        "parser_version",
        "evidence_basis_id",
    ):
        if type(values[key]) is not str:
            _fail("qualification_field")
    for key in ("model_size_bytes", "context_length"):
        if type(values[key]) is not int or isinstance(values[key], bool):
            _fail("qualification_field")
    return PromptModelQualificationEvidence(
        model_id=values["model_id"],
        model_digest=values["model_digest"],
        model_size_bytes=values["model_size_bytes"],
        model_format=values["model_format"],
        model_family=values["model_family"],
        parameter_size=values["parameter_size"],
        quantization_level=values["quantization_level"],
        context_length=values["context_length"],
        required_capabilities=tuple(capabilities),
        license_text_sha256=values["license_text_sha256"],
        show_identity_sha256=values["show_identity_sha256"],
        adapter_version=values["adapter_version"],
        parser_version=values["parser_version"],
        evidence_basis_id=values["evidence_basis_id"],
    )


def _decode_remote_qualification_evidence(
    values: object,
) -> RemotePromptModelQualificationEvidence | None:
    if values is None:
        return None
    if not isinstance(values, Mapping) or set(values) not in (
        _REMOTE_QUALIFICATION_EVIDENCE_KEYS,
        _REMOTE_QUALIFICATION_EVIDENCE_V2_KEYS,
    ):
        _fail("qualification_keys")
    basis = RemoteQualificationBasis.COMPLETION
    if "basis" in values:
        try:
            basis = RemoteQualificationBasis(values["basis"])
        except ValueError:
            _fail("qualification_basis")
    family_by_kind = {
        "remote_openai_compatible": PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        "remote_anthropic": PromptModelFamily.REMOTE_ANTHROPIC,
    }
    family = family_by_kind.get(values["kind"])
    if family is None:
        _fail("qualification_kind")
    for key in (
        "provider_id",
        "profile_id",
        "model_id",
        "policy_version",
        "policy_sha256",
        "price_basis_id",
        "qualified_on",
        "source_checked_on",
        "price_valid_through",
        "qualification_sha256",
        "repository_commit",
        "repository_tree",
        "adapter_version",
        "parser_version",
    ):
        if type(values[key]) is not str:
            _fail("remote_qualification_field")
    for key in (
        "max_transmissions",
        "observed_transmissions",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_micro_usd",
        "prompt_tokens",
        "completion_tokens",
        "actual_cost_micro_usd",
    ):
        if type(values[key]) is not int:
            _fail("remote_qualification_field")
    return RemotePromptModelQualificationEvidence(
        provider_id=values["provider_id"],
        profile_id=values["profile_id"],
        model_id=values["model_id"],
        policy_version=values["policy_version"],
        policy_sha256=values["policy_sha256"],
        price_basis_id=values["price_basis_id"],
        qualified_on=values["qualified_on"],
        source_checked_on=values["source_checked_on"],
        price_valid_through=values["price_valid_through"],
        max_transmissions=values["max_transmissions"],
        observed_transmissions=values["observed_transmissions"],
        max_input_tokens=values["max_input_tokens"],
        max_output_tokens=values["max_output_tokens"],
        max_cost_micro_usd=values["max_cost_micro_usd"],
        prompt_tokens=values["prompt_tokens"],
        completion_tokens=values["completion_tokens"],
        actual_cost_micro_usd=values["actual_cost_micro_usd"],
        qualification_sha256=values["qualification_sha256"],
        repository_commit=values["repository_commit"],
        repository_tree=values["repository_tree"],
        adapter_version=values["adapter_version"],
        parser_version=values["parser_version"],
        family=family,
        basis=basis,
    )


def _decode_profile_v2(values: object, seen: set[str]) -> PromptModelProfile:
    return _decode_profile_v2_or_later(values, seen, evidence_version=0)


def _decode_profile_v3(values: object, seen: set[str]) -> PromptModelProfile:
    return _decode_profile_v2_or_later(values, seen, evidence_version=3)


def _decode_profile_v4(values: object, seen: set[str]) -> PromptModelProfile:
    return _decode_profile_v2_or_later(values, seen, evidence_version=4)


def _decode_profile_v5(values: object, seen: set[str]) -> PromptModelProfile:
    return _decode_profile_v2_or_later(values, seen, evidence_version=5)


def _decode_profile_v2_or_later(
    values: object, seen: set[str], *, evidence_version: int
) -> PromptModelProfile:
    allow_qualified = evidence_version in {3, 4, 5}
    expected_keys = _PROFILE_V3_KEYS if allow_qualified else _PROFILE_V2_KEYS
    if not isinstance(values, Mapping) or set(values) != expected_keys:
        _fail("catalog_profile_keys")
    profile_id = _text(values, "profile_id", _PROFILE_ID, "catalog_profile_id")
    if profile_id in seen:
        _fail("catalog_duplicate_profile")
    seen.add(profile_id)

    raw_family = values["family"]
    if not isinstance(raw_family, str) or raw_family not in {
        item.value for item in PromptModelFamily
    }:
        _fail("catalog_family")
    family = PromptModelFamily(raw_family)
    if family is PromptModelFamily.REMOTE_ANTHROPIC and evidence_version < 5:
        # IMPORTANT: adding an enum member must not let an older catalog schema claim a dialect it
        # never defined. Only v5 can admit native Anthropic rows.
        _fail("catalog_family")
    capabilities = build_prompt_model_capabilities(values["capabilities"])
    if capabilities.family is not family:
        _fail("catalog_family")

    raw_dialect = values["wire_dialect"]
    try:
        dialect = PromptModelDialect(raw_dialect)
    except (TypeError, ValueError):
        _fail("catalog_dialect")
    expected_family = {
        PromptModelDialect.OLLAMA_CHAT: PromptModelFamily.OLLAMA,
        PromptModelDialect.OPENAI_CHAT_COMPLETIONS: PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
        PromptModelDialect.ANTHROPIC_MESSAGES: PromptModelFamily.REMOTE_ANTHROPIC,
    }.get(dialect)
    if expected_family is not family:
        _fail("catalog_dialect")

    endpoint = values["endpoint"]
    if not isinstance(endpoint, str):
        _fail("catalog_endpoint")
    admit_egress_destination(family, endpoint)

    discovery_allowed, chat_allowed = _DIALECT_ROUTES[dialect]
    discovery_routes = _closed_text_tuple(
        values["discovery_routes"],
        code="catalog_discovery_routes",
        maximum=4,
        allowed=discovery_allowed,
    )
    chat_route = values["chat_route"]
    if not isinstance(chat_route, str) or chat_route not in chat_allowed:
        _fail("catalog_chat_route")
    task_modes = _closed_text_tuple(
        values["task_modes"], code="catalog_task_modes", maximum=5, allowed=_TASK_MODES
    )

    raw_revision = values["model_revision"]
    if raw_revision is not None and (
        not isinstance(raw_revision, str)
        or not raw_revision
        or len(raw_revision) > 128
        or any(ord(char) < 0x20 for char in raw_revision)
    ):
        _fail("catalog_model_revision")
    raw_qualification = values["qualification_state"]
    try:
        qualification = PromptModelQualificationState(raw_qualification)
    except (TypeError, ValueError):
        _fail("catalog_qualification")
    if not allow_qualified and qualification is not PromptModelQualificationState.CATALOG_ONLY:
        _fail("catalog_qualification")
    if qualification is PromptModelQualificationState.LEGACY_UNQUALIFIED:
        _fail("catalog_qualification")
    evidence: PromptModelQualificationEvidence | RemotePromptModelQualificationEvidence | None = (
        None
    )
    if allow_qualified:
        raw_evidence = values["qualification_evidence"]
        if evidence_version == 3 or raw_evidence is None:
            evidence = _decode_local_qualification_evidence(raw_evidence, v4=evidence_version == 4)
        elif isinstance(raw_evidence, Mapping) and raw_evidence.get("kind") == "local_ollama":
            evidence = _decode_local_qualification_evidence(raw_evidence, v4=True)
        else:
            evidence = _decode_remote_qualification_evidence(raw_evidence)
        if (qualification is PromptModelQualificationState.QUALIFIED) is (evidence is None):
            _fail("catalog_qualification")
        # v3 was accepted as a local-only evidence contract. Preserve that meaning forever; only
        # v4's discriminated union can state a separately authorized remote qualification.
        if (
            evidence_version == 3
            and evidence is not None
            and family is not PromptModelFamily.OLLAMA
        ):
            _fail("catalog_qualification_family")

    cost_class = values["cost_class"]
    retention_policy = values["retention_policy"]
    if cost_class not in {"local_resource", "paid_remote"}:
        _fail("catalog_cost_class")
    if retention_policy not in {"local_process_only", "provider_policy"}:
        _fail("catalog_retention_policy")
    if (family is PromptModelFamily.OLLAMA) is not (cost_class == "local_resource"):
        _fail("catalog_cost_class")
    usage_receipt_required = values["usage_receipt_required"]
    if type(usage_receipt_required) is not bool:
        _fail("catalog_usage_receipt")
    if (
        family
        in {
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            PromptModelFamily.REMOTE_ANTHROPIC,
        }
        and not usage_receipt_required
    ):
        _fail("catalog_usage_receipt")

    return LegacyPromptModelProfile(
        profile_id=profile_id,
        family=family,
        endpoint=endpoint,
        model_id=_bounded_text(values, "model_id", "catalog_model_id"),
        model_digest=_optional_sha256(values["model_digest"], "catalog_digest"),
        adapter_version=_text(values, "adapter_version", _SAFE_VERSION, "catalog_version"),
        parser_version=_text(values, "parser_version", _SAFE_VERSION, "catalog_version"),
        license_id=_bounded_text(values, "license_id", "catalog_license", 64),
        license_source=_bounded_text(values, "license_source", "catalog_license", 128),
        license_text_sha256=_optional_sha256(values["license_text_sha256"], "catalog_digest"),
        capabilities=capabilities,
        provider_label=_bounded_text(values, "provider_label", "catalog_provider_label", 64),
        wire_dialect=dialect,
        discovery_routes=discovery_routes,
        chat_route=chat_route,
        task_modes=task_modes,
        model_revision=raw_revision,
        request_timeout_seconds=_v2_bounded_int(
            values, "request_timeout_seconds", minimum=1, maximum=300
        ),
        max_retries=_v2_bounded_int(values, "max_retries", minimum=0, maximum=1),
        max_concurrency=_v2_bounded_int(values, "max_concurrency", minimum=1, maximum=4),
        max_calls_per_action=_v2_bounded_int(values, "max_calls_per_action", minimum=1, maximum=4),
        cost_class=cost_class,
        usage_receipt_required=usage_receipt_required,
        retention_policy=retention_policy,
        qualification_state=qualification,
        qualification_evidence=evidence,
        limitations=_closed_text_tuple(
            values["limitations"], code="catalog_limitations", maximum=8
        ),
    )


def _decode_profile_v6(values: object, seen: set[str]) -> PromptModelProfile:
    """Validate a model-free connection without widening the historical pinned schemas."""

    model_keys = {
        "model_id",
        "model_revision",
        "model_digest",
        "license_id",
        "license_source",
        "license_text_sha256",
    }
    expected_keys = _PROFILE_V3_KEYS - model_keys
    if not isinstance(values, Mapping) or set(values) != expected_keys:
        _fail("catalog_profile_keys")
    bindings = {
        "ollama.local": (PromptModelFamily.OLLAMA, "http://127.0.0.1:11434"),
        "openai.remote": (PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, "https://api.openai.com"),
        "anthropic.remote": (PromptModelFamily.REMOTE_ANTHROPIC, "https://api.anthropic.com"),
        "gemini.remote": (
            PromptModelFamily.REMOTE_OPENAI_COMPATIBLE,
            "https://generativelanguage.googleapis.com",
        ),
    }
    profile_id = values["profile_id"]
    if not isinstance(profile_id, str):
        _fail("catalog_connection")
    binding = bindings.get(profile_id)
    if binding is None or (values["family"], values["endpoint"]) != (binding[0].value, binding[1]):
        _fail("catalog_connection")
    # IMPORTANT: reuse validation only, with qualification disabled. Synthetic historical model
    # fields never escape into the connection or create model/qualification execution authority.
    validation = dict(values)
    native_routes = {
        "ollama.local": "/api/tags",
        "openai.remote": "/v1/models",
        "gemini.remote": "/v1beta/models?pageSize=1000",
        "anthropic.remote": "/v1/models?limit=1000",
    }
    expected_native_routes = (
        ["/api/tags", "/api/show"] if profile_id == "ollama.local" else [native_routes[profile_id]]
    )
    if values["discovery_routes"] != expected_native_routes:
        _fail("catalog_discovery_routes")
    # CRITICAL: validate new fixed native queries only in v6. Expanding the sealed v5 route
    # allowlist would grant historical profiles a protocol they never declared or qualified.
    validation["discovery_routes"] = [
        "/v1beta/openai/models"
        if profile_id == "gemini.remote"
        else "/v1/models"
        if profile_id == "anthropic.remote"
        else native_routes[profile_id]
    ]
    if profile_id == "ollama.local":
        validation["discovery_routes"] = expected_native_routes
        validation["max_calls_per_action"] = 2
    validation.update(
        model_id="validation-placeholder",
        model_digest=None,
        model_revision=None,
        license_id="not_applicable",
        license_source="not_applicable",
        license_text_sha256=None,
        qualification_state="catalog_only",
        qualification_evidence=None,
    )
    checked = _decode_profile_v5(validation, seen)
    raw_evidence = values["qualification_evidence"]
    evidence = None
    if raw_evidence is not None:
        evidence_keys = {
            "kind",
            "profile_id",
            "family",
            "observation_model_id",
            "adapter_version",
            "parser_version",
            "observed_on",
            "evidence_basis_sha256",
            "max_transmissions",
        }
        if not isinstance(raw_evidence, Mapping) or set(raw_evidence) != evidence_keys:
            _fail("catalog_connection_qualification")
        if raw_evidence["kind"] != "provider_connection":
            _fail("catalog_connection_qualification")
        if any(type(raw_evidence[key]) is not str for key in evidence_keys - {"max_transmissions"}):
            _fail("catalog_connection_qualification")
        try:
            evidence = ConnectionQualificationEvidence(
                profile_id=raw_evidence["profile_id"],
                family=PromptModelFamily(raw_evidence["family"]),
                observation_model_id=raw_evidence["observation_model_id"],
                adapter_version=raw_evidence["adapter_version"],
                parser_version=raw_evidence["parser_version"],
                observed_on=raw_evidence["observed_on"],
                evidence_basis_sha256=raw_evidence["evidence_basis_sha256"],
                max_transmissions=raw_evidence["max_transmissions"],
            )
            qualification = PromptModelQualificationState(values["qualification_state"])
        except (TypeError, ValueError):
            _fail("catalog_connection_qualification")
    else:
        if values["qualification_state"] != PromptModelQualificationState.CATALOG_ONLY.value:
            _fail("catalog_connection_qualification")
        qualification = PromptModelQualificationState.CATALOG_ONLY
    data = {item.name: getattr(checked, item.name) for item in fields(PromptModelProfile)}
    data.update(
        qualification_state=qualification,
        qualification_evidence=evidence,
        discovery_routes=tuple(expected_native_routes),
        max_calls_per_action=values["max_calls_per_action"],
    )
    if checked.max_retries != 0 or checked.max_concurrency != 1:
        _fail("catalog_connection_limits")
    if values["max_calls_per_action"] != 4:
        _fail("catalog_connection_limits")
    return PromptModelProfile(**data)


def decode_prompt_model_catalog(raw: bytes) -> PromptModelCatalog:
    """Decode a pinned catalog. Every endpoint is judged by the chokepoint before it is stored."""

    if not isinstance(raw, bytes | bytearray):
        _fail("catalog_shape")
    if len(raw) > MAX_CATALOG_BYTES:
        _fail("catalog_size")
    try:
        decoded = json.loads(
            bytes(raw).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except PromptModelContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptModelContractError("catalog_json") from exc

    if not isinstance(decoded, dict):
        _fail("catalog_root")
    schema = decoded.get("schema")
    if schema not in {
        PROMPT_MODEL_CATALOG_V1_SCHEMA,
        PROMPT_MODEL_CATALOG_V2_SCHEMA,
        PROMPT_MODEL_CATALOG_V3_SCHEMA,
        PROMPT_MODEL_CATALOG_V4_SCHEMA,
        PROMPT_MODEL_CATALOG_V5_SCHEMA,
        PROMPT_MODEL_CATALOG_SCHEMA,
    }:
        _fail("catalog_schema")
    expected_root = (
        _CATALOG_V1_ROOT_KEYS if schema == PROMPT_MODEL_CATALOG_V1_SCHEMA else _CATALOG_V2_ROOT_KEYS
    )
    if set(decoded) != expected_root:
        _fail("catalog_root")
    if schema != PROMPT_MODEL_CATALOG_V1_SCHEMA and decoded["default_profile_id"] is not None:
        _fail("catalog_default")
    values = decoded["profiles"]
    if type(values) is not list or len(values) > MAX_CATALOG_PROFILES:
        _fail("catalog_profile_count")

    seen: set[str] = set()
    decoder = {
        PROMPT_MODEL_CATALOG_V1_SCHEMA: _decode_profile_v1,
        PROMPT_MODEL_CATALOG_V2_SCHEMA: _decode_profile_v2,
        PROMPT_MODEL_CATALOG_V3_SCHEMA: _decode_profile_v3,
        PROMPT_MODEL_CATALOG_V4_SCHEMA: _decode_profile_v4,
        PROMPT_MODEL_CATALOG_V5_SCHEMA: _decode_profile_v5,
        PROMPT_MODEL_CATALOG_SCHEMA: _decode_profile_v6,
    }[schema]
    profiles = tuple(decoder(item, seen) for item in values)
    return PromptModelCatalog(schema=schema, profiles=profiles, default_profile_id=None)


def load_prompt_model_catalog() -> PromptModelCatalog:
    """Load package-owned capability rows. Catalog membership never selects a default."""

    try:
        return decode_prompt_model_catalog(PROMPT_MODEL_CATALOG_PATH.read_bytes())
    except OSError:
        raise PromptModelContractError("catalog_unavailable") from None


def load_legacy_prompt_model_catalog() -> PromptModelCatalog:
    """Read sealed v5 for historical tools/decoders; never use it for current Settings authority."""
    try:
        return decode_prompt_model_catalog(
            PROMPT_MODEL_CATALOG_PATH.with_name("prompt_model_profiles_v5.json").read_bytes()
        )
    except OSError:
        raise PromptModelContractError("catalog_unavailable") from None


def prompt_model_families() -> tuple[PromptModelFamily, ...]:
    """The declared families, in declaration order. Enumeration is not selection."""

    return tuple(PROMPT_MODEL_FAMILY_MATRIX)


def prompt_model_outcome_ids() -> tuple[str, ...]:
    """The closed outcome registry as an ordered tuple, for catalogue and locale coverage checks."""

    return tuple(item.value for item in PromptModelOutcomeId)
