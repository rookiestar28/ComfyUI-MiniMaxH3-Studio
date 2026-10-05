"""M22-04 consented remote prompt-model providers: what may leave the machine, and when.

Nothing here transmits. This module decides whether a transmission is allowed, what a credential is
allowed to be, what may come back from a third party, and what the receipt of a completed call may
say. The transport that acts on those decisions is in `adapters/prompt_model_transport.py`.

Four things must all be true before a byte leaves, and they are checked together immediately before
transmission rather than remembered from setup time: the user selected this exact profile, the user
permitted network use, the user consented to upload when media is involved, and a credential exists
for this session. Consent is revocable and a revocation takes effect on the very next transmission,
because a consent that only applies at connect time is a consent the user cannot withdraw.

The credential never becomes data. `RuntimeCredential` refuses `repr`, `str`, iteration, comparison,
copying, pickling and every mapping conversion, so it cannot be reached through a log line, an
exception traceback, `dataclasses.asdict`, a cache key or a debugger dump of a frame. The only thing
that ever leaves it is one header value handed straight to the transport.

Upstream text is not product copy and not a data source. An error body is reduced to a declared,
closed subset -- a status, one recognised error code, and at most one short scrubbed sentence marked
untrusted -- and everything else is dropped. The outcome the product acts on comes from the status
and the recognised code, never from the sentence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn

from .contracts import ValidationSeverity
from .prompt_model_provider import (
    PromptModelContractError,
    PromptModelCredentialSource,
    PromptModelFamily,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelRemediation,
    UntrustedProviderDetail,
    build_prompt_model_outcome,
)
from .provider_setup import ProviderConsentStatus
from .remote_provider_policy import RemoteCostAuthority

REMOTE_PROMPT_MODEL_SCHEMA = "h3-context-remote-prompt-model/1"

# Compatibility alias for the first remote family, plus the closed set that shares consent and
# egress semantics. Dialect-owned headers and bodies remain separate in the adapter.
REMOTE_FAMILY = PromptModelFamily.REMOTE_OPENAI_COMPATIBLE
REMOTE_FAMILIES: frozenset[PromptModelFamily] = frozenset(
    {PromptModelFamily.REMOTE_OPENAI_COMPATIBLE, PromptModelFamily.REMOTE_ANTHROPIC}
)
REMOTE_FAMILY_ENABLED_BY_DEFAULT = False

MAX_CREDENTIAL_CHARACTERS = 512
MAX_RECEIPT_IDENTIFIER = 128
MAX_UPSTREAM_DETAIL_CHARACTERS = 256

# Upstream error codes this repository recognises. Anything outside the set is dropped rather than
# forwarded, so an unfamiliar code can never steer a decision or reach a display surface.
SAFE_UPSTREAM_ERROR_CODES: frozenset[str] = frozenset(
    {
        "insufficient_quota",
        "rate_limit_exceeded",
        "invalid_api_key",
        "invalid_request_error",
        "context_length_exceeded",
        "content_policy_violation",
        "model_not_found",
        "server_error",
        "authentication_error",
        "billing_error",
        "permission_error",
        "not_found_error",
        "conflict_error",
        "request_too_large",
        "rate_limit_error",
        "api_error",
        "timeout_error",
        "overloaded_error",
    }
)


def _fail(code: str) -> NoReturn:
    raise PromptModelContractError(code)


def _identifier(value: object, code: str, maximum: int = MAX_RECEIPT_IDENTIFIER) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(code)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(code)
    return value


def _count(value: object, code: str, maximum: int = 1 << 40) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail(code)
    return value


class RemoteRefusal(str, Enum):
    """Why a transmission was refused before anything was sent."""

    NOT_SELECTED = "profile_not_selected"
    CONSENT_MISSING = "consent_missing"
    CONSENT_REVOKED = "consent_revoked"
    NETWORK_NOT_PERMITTED = "network_not_permitted"
    UPLOAD_NOT_CONSENTED = "upload_not_consented"
    CREDENTIAL_ABSENT = "credential_absent"
    WRONG_FAMILY = "wrong_family"


_REFUSAL_OUTCOMES: Mapping[RemoteRefusal, PromptModelOutcomeId] = {
    RemoteRefusal.NOT_SELECTED: PromptModelOutcomeId.CONSENT_REQUIRED,
    RemoteRefusal.CONSENT_MISSING: PromptModelOutcomeId.CONSENT_REQUIRED,
    RemoteRefusal.CONSENT_REVOKED: PromptModelOutcomeId.CONSENT_REVOKED,
    RemoteRefusal.NETWORK_NOT_PERMITTED: PromptModelOutcomeId.NETWORK_NOT_PERMITTED,
    RemoteRefusal.UPLOAD_NOT_CONSENTED: PromptModelOutcomeId.UPLOAD_NOT_CONSENTED,
    RemoteRefusal.CREDENTIAL_ABSENT: PromptModelOutcomeId.AUTHENTICATION,
    RemoteRefusal.WRONG_FAMILY: PromptModelOutcomeId.CAPABILITY_MISMATCH,
}

_REFUSAL_REMEDIATIONS: Mapping[RemoteRefusal, PromptModelRemediation] = {
    RemoteRefusal.NOT_SELECTED: PromptModelRemediation.SELECT_MODEL,
    RemoteRefusal.CONSENT_MISSING: PromptModelRemediation.GRANT_CONSENT,
    RemoteRefusal.CONSENT_REVOKED: PromptModelRemediation.GRANT_CONSENT,
    RemoteRefusal.NETWORK_NOT_PERMITTED: PromptModelRemediation.GRANT_CONSENT,
    RemoteRefusal.UPLOAD_NOT_CONSENTED: PromptModelRemediation.GRANT_CONSENT,
    RemoteRefusal.CREDENTIAL_ABSENT: PromptModelRemediation.REVIEW_CREDENTIAL,
    RemoteRefusal.WRONG_FAMILY: PromptModelRemediation.SELECT_MODEL,
}


class RuntimeCredential:
    """A secret that exists only in this process and refuses to become data by accident.

    What is closed here is every route by which a value escapes without anyone meaning it to:
    printing it, formatting it into a log line or an f-string, comparing it, hashing it into a
    cache key, iterating it, copying it, pickling it, converting it to a mapping, or having it
    surface in an exception's arguments or a traceback.

    What is *not* closed, and cannot be in this language, is deliberate introspection from inside
    the same process: `gc.get_referents` on this object returns the slot contents, and the slot is
    reachable by name. That is the same privilege level at which the attribute could simply be read,
    so it is a documented boundary rather than a defended one. Code running in this interpreter can
    read this secret; the guarantee is that nothing reaches it by ordinary use, and that no
    serialization, display or comparison path can carry it somewhere it was never meant to go.

    The only intended exits are the scheme-specific `authorization_header()` and
    `api_key_header()` methods. A caller selects exactly one from declared provider policy, hands
    its result straight to transport, and does not store it.
    """

    __slots__ = ("_secret",)

    _secret: str

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or not secret or len(secret) > MAX_CREDENTIAL_CHARACTERS:
            _fail("credential_value")
        # SECURITY: an HTTP header value is latin-1 encoded, so a character above U+00FF cannot be
        # transmitted at all. Accepting one moved the failure from here to `http.client.putheader`
        # at transmission time, where `UnicodeEncodeError` derives from `ValueError`, matches none
        # of the transport's except clauses, and escapes the provider boundary uncaught -- carrying
        # the whole `Authorization: Bearer <secret>` value in its `args` and `object`, which is
        # exactly the route this class documents as closed. Invisible characters travel with copy
        # and paste: a zero-width space, a BOM from an editor, a smart quote from a chat client.
        # Refusing here puts the failure back where `ProviderSettingsState` already turns it into a
        # content-free CREDENTIAL_REJECTED.
        #
        # An ordinal comparison rather than a trial `encode("latin-1")`, deliberately: the trial
        # would construct the very object being prevented, an exception holding the secret, and a
        # `bytes` copy of it on the success path. This comparison creates neither. 0x80-0xFF stays
        # accepted -- it is `obs-text` under RFC 7230 and is transmissible, so it is not this
        # item's business.
        if any(ord(char) < 0x20 or ord(char) == 0x7F or ord(char) > 0xFF for char in secret):
            _fail("credential_value")
        object.__setattr__(self, "_secret", secret)

    @property
    def source(self) -> PromptModelCredentialSource:
        return PromptModelCredentialSource.SESSION_ONLY

    def authorization_header(self) -> str:
        """The Bearer-scheme exit. Handed straight to a transport and never stored."""

        return f"Bearer {self._secret}"

    def api_key_header(self) -> str:
        """The native API-key exit. Handed straight to a transport and never stored."""

        return self._secret

    def __repr__(self) -> str:
        return "<RuntimeCredential redacted>"

    def __str__(self) -> str:
        return "<RuntimeCredential redacted>"

    def __format__(self, _spec: str) -> str:
        return "<RuntimeCredential redacted>"

    def __eq__(self, _other: object) -> bool:
        # Comparison is an oracle: a caller could recover a secret one guess at a time.
        _fail("credential_comparison")

    def __hash__(self) -> int:
        _fail("credential_hash")

    def __iter__(self) -> NoReturn:
        _fail("credential_iteration")

    def __reduce__(self) -> NoReturn:
        _fail("credential_serialization")

    def __copy__(self) -> NoReturn:
        _fail("credential_serialization")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        _fail("credential_serialization")

    def __getstate__(self) -> NoReturn:
        _fail("credential_serialization")


@dataclass(frozen=True, slots=True)
class RemoteConsentRecord:
    """One provider-scoped consent decision, as recorded and as revocable."""

    profile_id: str
    status: ProviderConsentStatus
    network_permitted: bool
    media_upload_consented: bool
    cost_authority: RemoteCostAuthority | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "consent_profile_id")
        if not isinstance(self.status, ProviderConsentStatus):
            _fail("consent_status")
        if self.status is ProviderConsentStatus.NOT_REQUIRED:
            # The remote family always requires consent; recording "not required" for it would be a
            # decision the user never made.
            _fail("consent_status")
        for name in ("network_permitted", "media_upload_consented"):
            if type(getattr(self, name)) is not bool:
                _fail("consent_flag")
        if self.cost_authority is not None and not isinstance(
            self.cost_authority, RemoteCostAuthority
        ):
            _fail("consent_cost_authority")
        if type(self.revision) is not int or not 1 <= self.revision <= 2_147_483_647:
            _fail("consent_revision")

    @property
    def granted(self) -> bool:
        return self.status is ProviderConsentStatus.GRANTED

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_PROMPT_MODEL_SCHEMA,
            "profile_id": self.profile_id,
            "status": self.status.value,
            "network_permitted": self.network_permitted,
            "media_upload_consented": self.media_upload_consented,
            "cost_authority": (
                None if self.cost_authority is None else self.cost_authority.to_wire()
            ),
            "revision": self.revision,
        }


class RemoteConsentLedger:
    """In-memory, session-scoped consent. Nothing here is persisted, so nothing survives a restart.

    A consent that outlived the session would be a decision the user is no longer present to
    reconsider, which is the opposite of what an explicit gate is for.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, RemoteConsentRecord] = {}

    def grant(
        self,
        profile_id: str,
        *,
        network_permitted: bool,
        media_upload_consented: bool,
        cost_authority: RemoteCostAuthority | None = None,
    ) -> RemoteConsentRecord:
        _identifier(profile_id, "consent_profile_id")
        previous = self._records.get(profile_id)
        record = RemoteConsentRecord(
            profile_id=profile_id,
            status=ProviderConsentStatus.GRANTED,
            network_permitted=network_permitted,
            media_upload_consented=media_upload_consented,
            cost_authority=cost_authority,
            revision=1 if previous is None else previous.revision + 1,
        )
        self._records[profile_id] = record
        return record

    def revoke(self, profile_id: str) -> RemoteConsentRecord:
        _identifier(profile_id, "consent_profile_id")
        previous = self._records.get(profile_id)
        record = RemoteConsentRecord(
            profile_id=profile_id,
            status=ProviderConsentStatus.DENIED,
            network_permitted=False,
            media_upload_consented=False,
            cost_authority=None,
            revision=1 if previous is None else previous.revision + 1,
        )
        self._records[profile_id] = record
        return record

    def record_for(self, profile_id: object) -> RemoteConsentRecord | None:
        if not isinstance(profile_id, str):
            _fail("consent_profile_id")
        return self._records.get(profile_id)

    @property
    def granted_profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, value in self._records.items() if value.granted))


@dataclass(frozen=True, slots=True)
class RemoteTransmissionDecision:
    """The answer to "may this specific request leave the machine right now"."""

    outcome: PromptModelOutcome
    refusal: RemoteRefusal | None

    @property
    def admitted(self) -> bool:
        return self.refusal is None


def _refuse(refusal: RemoteRefusal, profile_id: str) -> RemoteTransmissionDecision:
    return RemoteTransmissionDecision(
        outcome=build_prompt_model_outcome(
            _REFUSAL_OUTCOMES[refusal],
            severity=ValidationSeverity.ERROR,
            remediation=_REFUSAL_REMEDIATIONS[refusal],
            parameters=(("refusal", refusal.value), ("profile_id", profile_id)),
        ),
        refusal=refusal,
    )


def admit_remote_transmission(
    *,
    profile: object,
    selected_profile_id: object,
    consent: object,
    credential: object,
    carries_media: object,
) -> RemoteTransmissionDecision:
    """The gate, evaluated immediately before transmission and never cached from setup time."""

    if not isinstance(profile, PromptModelProfile):
        _fail("transmission_profile")
    if type(carries_media) is not bool:
        _fail("transmission_media_flag")
    if profile.family not in REMOTE_FAMILIES:
        return _refuse(RemoteRefusal.WRONG_FAMILY, profile.profile_id)
    if not isinstance(selected_profile_id, str) or selected_profile_id != profile.profile_id:
        return _refuse(RemoteRefusal.NOT_SELECTED, profile.profile_id)
    if consent is None:
        return _refuse(RemoteRefusal.CONSENT_MISSING, profile.profile_id)
    if not isinstance(consent, RemoteConsentRecord):
        _fail("transmission_consent")
    if consent.profile_id != profile.profile_id:
        return _refuse(RemoteRefusal.NOT_SELECTED, profile.profile_id)
    if consent.status is ProviderConsentStatus.DENIED:
        return _refuse(RemoteRefusal.CONSENT_REVOKED, profile.profile_id)
    if not consent.granted:
        return _refuse(RemoteRefusal.CONSENT_MISSING, profile.profile_id)
    if not consent.network_permitted:
        return _refuse(RemoteRefusal.NETWORK_NOT_PERMITTED, profile.profile_id)
    if carries_media and not consent.media_upload_consented:
        return _refuse(RemoteRefusal.UPLOAD_NOT_CONSENTED, profile.profile_id)
    if not isinstance(credential, RuntimeCredential):
        return _refuse(RemoteRefusal.CREDENTIAL_ABSENT, profile.profile_id)
    return RemoteTransmissionDecision(
        outcome=build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(
                ("profile_id", profile.profile_id),
                ("consent_revision", consent.revision),
                ("media", carries_media),
            ),
        ),
        refusal=None,
    )


@dataclass(frozen=True, slots=True)
class UpstreamError:
    """What survives from a third party's error body: a status, maybe a code, maybe a sentence."""

    status: int
    code: str | None
    detail: UntrustedProviderDetail | None

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            _fail("upstream_status")
        if self.code is not None and self.code not in SAFE_UPSTREAM_ERROR_CODES:
            _fail("upstream_code")
        if self.detail is not None and not isinstance(self.detail, UntrustedProviderDetail):
            _fail("upstream_detail")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_PROMPT_MODEL_SCHEMA,
            "status": self.status,
            "code": self.code,
            "untrusted_provider_detail": (None if self.detail is None else self.detail.to_wire()),
        }


def scrub_upstream_error(status: object, payload: object) -> UpstreamError:
    """Reduce an upstream body to the declared safe subset. Everything unrecognised is dropped."""

    if type(status) is not int or not 100 <= status <= 599:
        _fail("upstream_status")
    code: str | None = None
    message: str | None = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        source: Mapping[str, Any] = error if isinstance(error, Mapping) else payload
        raw_code = source.get("code")
        if isinstance(raw_code, str) and raw_code in SAFE_UPSTREAM_ERROR_CODES:
            code = raw_code
        if code is None:
            raw_type = source.get("type")
            if isinstance(raw_type, str) and raw_type in SAFE_UPSTREAM_ERROR_CODES:
                code = raw_type
        raw_message = source.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message[:MAX_UPSTREAM_DETAIL_CHARACTERS]
    detail = None if message is None else UntrustedProviderDetail.from_provider_text(message)
    if detail is not None and not detail.text:
        detail = None
    return UpstreamError(status=status, code=code, detail=detail)


_STATUS_OUTCOMES: Mapping[int, PromptModelOutcomeId] = {
    400: PromptModelOutcomeId.PROVIDER_ERROR,
    401: PromptModelOutcomeId.AUTHENTICATION,
    402: PromptModelOutcomeId.PAYMENT_REQUIRED,
    403: PromptModelOutcomeId.PERMISSION_DENIED,
    404: PromptModelOutcomeId.MODEL_MISSING,
    408: PromptModelOutcomeId.TIMEOUT,
    413: PromptModelOutcomeId.REQUEST_TOO_LARGE,
    415: PromptModelOutcomeId.UNSUPPORTED_MEDIA,
    429: PromptModelOutcomeId.RATE_LIMITED,
}

_CODE_OUTCOMES: Mapping[str, PromptModelOutcomeId] = {
    "insufficient_quota": PromptModelOutcomeId.QUOTA,
    "rate_limit_exceeded": PromptModelOutcomeId.RATE_LIMITED,
    "invalid_api_key": PromptModelOutcomeId.AUTHENTICATION,
    "context_length_exceeded": PromptModelOutcomeId.CONTEXT_EXCEEDED,
    "content_policy_violation": PromptModelOutcomeId.MODERATED,
    "model_not_found": PromptModelOutcomeId.MODEL_MISSING,
    "authentication_error": PromptModelOutcomeId.AUTHENTICATION,
    "billing_error": PromptModelOutcomeId.PAYMENT_REQUIRED,
    "permission_error": PromptModelOutcomeId.PERMISSION_DENIED,
    "not_found_error": PromptModelOutcomeId.MODEL_MISSING,
    "conflict_error": PromptModelOutcomeId.PROVIDER_ERROR,
    "request_too_large": PromptModelOutcomeId.REQUEST_TOO_LARGE,
    "rate_limit_error": PromptModelOutcomeId.RATE_LIMITED,
    "api_error": PromptModelOutcomeId.PROVIDER_ERROR,
    "timeout_error": PromptModelOutcomeId.TIMEOUT,
    "overloaded_error": PromptModelOutcomeId.PROVIDER_ERROR,
}


def map_remote_outcome(error: object) -> PromptModelOutcomeId:
    """Classify a failed remote call from its status and its recognised code, never from prose.

    The code wins where one is recognised, because a status alone cannot tell an account that ran
    out of credit from one that is merely being asked to slow down -- both arrive as 429, and the
    two need opposite responses from the user.
    """

    if not isinstance(error, UpstreamError):
        _fail("upstream_error")
    if error.code is not None and error.code in _CODE_OUTCOMES:
        return _CODE_OUTCOMES[error.code]
    mapped = _STATUS_OUTCOMES.get(error.status)
    if mapped is not None:
        return mapped
    if 500 <= error.status <= 599:
        return PromptModelOutcomeId.PROVIDER_ERROR
    if 300 <= error.status <= 399:
        return PromptModelOutcomeId.REDIRECT_REFUSED
    return PromptModelOutcomeId.PROVIDER_ERROR


@dataclass(frozen=True, slots=True)
class RemoteUsageReceipt:
    """What a completed remote call is allowed to record. Counts and identifiers, never content."""

    profile_id: str
    host: str
    outcome_id: PromptModelOutcomeId
    http_status: int
    request_bytes: int
    response_bytes: int
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    credential_last_four: str = ""
    provider_id: str = ""
    model_id: str = ""
    policy_sha256: str = ""
    price_basis_id: str = ""
    maximum_cost_micro_usd: int = 0
    actual_cost_micro_usd: int = 0
    usage_present: bool = False

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "receipt_profile_id")
        _identifier(self.host, "receipt_host")
        if not isinstance(self.outcome_id, PromptModelOutcomeId):
            _fail("receipt_outcome")
        if type(self.http_status) is not int or not 0 <= self.http_status <= 599:
            _fail("receipt_status")
        for name in (
            "request_bytes",
            "response_bytes",
            "prompt_tokens",
            "completion_tokens",
            "duration_ms",
        ):
            _count(getattr(self, name), "receipt_count")
        if self.credential_last_four != "":
            _fail("receipt_credential_hint")
        for name in ("provider_id", "model_id", "policy_sha256", "price_basis_id"):
            value = getattr(self, name)
            if value:
                _identifier(value, "receipt_policy_identity")
        for name in ("maximum_cost_micro_usd", "actual_cost_micro_usd"):
            _count(getattr(self, name), "receipt_cost")
        if type(self.usage_present) is not bool:
            _fail("receipt_usage_present")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": REMOTE_PROMPT_MODEL_SCHEMA,
            "profile_id": self.profile_id,
            "host": self.host,
            "outcome_id": self.outcome_id.value,
            "http_status": self.http_status,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": self.duration_ms,
            "credential_last_four": self.credential_last_four,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "policy_sha256": self.policy_sha256,
            "price_basis_id": self.price_basis_id,
            "maximum_cost_micro_usd": self.maximum_cost_micro_usd,
            "actual_cost_micro_usd": self.actual_cost_micro_usd,
            "usage_present": self.usage_present,
        }


def remote_family_is_enabled_by_default() -> bool:
    """Always false. Kept as a function so a test can assert the answer rather than a comment."""

    return REMOTE_FAMILY_ENABLED_BY_DEFAULT
