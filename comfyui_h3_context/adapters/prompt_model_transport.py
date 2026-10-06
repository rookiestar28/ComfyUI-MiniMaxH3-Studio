"""M22-03 local loopback prompt-model transports and the crash-isolated capability probe.

Two families are carried here: an externally managed OpenAI-compatible server and the Ollama daemon,
both loopback-only. This module opens sockets and starts a subprocess; every decision about whether
it *may* is made in `core/prompt_model_session.py`, which needs no server to test.

The transport is constructed from an `AdmittedDestination`, never from a URL string. That is what
makes "no non-loopback destination on this path" structural rather than a check someone has to
remember: a local family's admitted destination is loopback by construction, and there is no string
overload through which another host could arrive.

This repository never starts, stops or supervises the user's server. It probes, it uses, and it
reports. The native capability probe runs in a separate interpreter with a bounded timeout and a
minimal environment, and every way it can fail blocks the family rather than degrading to an
optimistic assumption.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from ipaddress import IPv4Address, IPv6Address, ip_address
from math import isfinite
from typing import Any, cast

from ..core.contracts import ValidationSeverity
from ..core.prompt_model_dialects import DialectResponseError, RequestOptions
from ..core.prompt_model_dialects.discovery import read_native_models
from ..core.prompt_model_execution_budget import chosen_model_budget, options_for_metadata
from ..core.prompt_model_provider import (
    MAX_DISCOVERY_ROWS,
    AdmittedDestination,
    ExactTagsRow,
    LegacyPromptModelProfile,
    ModelChoice,
    PromptModelContractError,
    PromptModelFamily,
    PromptModelOutcome,
    PromptModelOutcomeId,
    PromptModelProfile,
    PromptModelQualificationState,
    PromptModelRemediation,
    ShowIdentity,
    UntrustedProviderDetail,
    admit_egress_destination,
    build_prompt_model_outcome,
    normalize_ollama_digest,
    parse_exact_tags_row,
    parse_show_identity,
    remediation_for,
)
from ..core.prompt_model_session import (
    OPENAI_COMPATIBLE_FAMILIES,
    LiveModelIdentity,
    NativeProbeReport,
    NativeProbeStatus,
    PromptModelAnswer,
    PromptModelSessionRequest,
    admit_session_request,
    build_request_payload,
    read_response_answer,
    verify_live_identity,
)
from ..core.remote_prompt_model import (
    REMOTE_FAMILIES,
    RemoteTransmissionDecision,
    RemoteUsageReceipt,
    RuntimeCredential,
    admit_remote_transmission,
    map_remote_outcome,
    scrub_upstream_error,
)
from ..core.remote_provider_policy import (
    RemoteCredentialScheme,
    RemoteProviderPolicy,
    policy_for_profile,
    remote_provider_policies,
)

MAX_PROMPT_MODEL_REQUEST_BYTES = 1_000_000
MAX_PROMPT_MODEL_RESPONSE_BYTES = 4_000_000
MAX_TIMEOUT_SECONDS = 300.0
MAX_PROBE_OUTPUT_BYTES = 4_096
MAX_PROBE_TIMEOUT_SECONDS = 60.0
MAX_RESOLUTION_ADDRESSES = 32
MAX_RESOLUTION_OUTPUT_BYTES = 4_096

# M22-13 adds `/api/show` and stops there. `/api/pull`, `/api/create`, `/api/delete` and
# `/api/generate` are absent by intent, not by oversight: this lane observes and drafts, and
# an allowlist is the only thing standing between a compromised catalog row and a route that
# mutates the operator's model store.
OLLAMA_PROMPT_PATHS = frozenset({"/api/tags", "/api/show", "/api/chat"})
LOOPBACK_SERVER_PROMPT_PATHS = frozenset({"/v1/models", "/v1/chat/completions"})

FAMILY_PATHS: Mapping[PromptModelFamily, frozenset[str]] = {
    PromptModelFamily.OLLAMA: OLLAMA_PROMPT_PATHS,
    PromptModelFamily.LOOPBACK_SERVER: LOOPBACK_SERVER_PROMPT_PATHS,
}

# Importing a module executes it, so the probe may only import runtimes this repository has
# actually reasoned about. An unlisted name is refused rather than probed.
PROBEABLE_RUNTIMES = frozenset({"llama_cpp"})

_PROBE_SOURCE = """
import importlib, json, sys
name = sys.argv[1] if len(sys.argv) > 1 else ""
module = importlib.import_module(name)
version = getattr(module, "__version__", "")
backends = []
for flag, label in (
    ("GGML_USE_CUDA", "cuda"),
    ("GGML_USE_METAL", "metal"),
    ("GGML_USE_VULKAN", "vulkan"),
):
    if getattr(module, flag, False):
        backends.append(label)
sys.stdout.write(
    json.dumps({"runtime_version": str(version)[:64], "detected_backends": backends[:8]})
)
"""

_RESOLUTION_SOURCE = """
import json, socket, sys
host = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
except OSError:
    raise SystemExit(2)
addresses = []
for info in infos:
    candidate = info[4][0]
    if candidate not in addresses:
        addresses.append(candidate)
sys.stdout.write(json.dumps({"addresses": addresses}, separators=(",", ":")))
"""


class PromptModelTransportError(RuntimeError):
    """A transport-level failure that the caller turns into a typed outcome."""

    def __init__(
        self,
        outcome_id: PromptModelOutcomeId,
        detail: str = "",
        *,
        unexpected_type: str | None = None,
        http_status: int = 0,
        error_kind: str = "",
    ) -> None:
        self.outcome_id = outcome_id
        self.detail = detail
        self.http_status = http_status
        # Only allowlisted protocol discriminators survive. Never store error.message here.
        self.error_kind = (
            error_kind if error_kind in {"invalid_request_error", "INVALID_ARGUMENT"} else ""
        )
        # M22-23. Set only by the boundary backstop, and read by nobody: `detail` reaches the user
        # as provider-attributed prose, so the class name of an exception this repository did not
        # anticipate cannot go there without telling the user the provider said something it did
        # not. It is kept here instead so a novel failure mode stays nameable to a developer
        # holding the exception. A class `__name__` is an identifier fixed at definition time and
        # cannot carry provider content or a credential.
        self.unexpected_type = unexpected_type
        super().__init__(outcome_id.value)


def _timeout(value: object, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "timeout")
    try:
        seconds = float(value)
    except OverflowError as exc:
        # M22-23. An int too large to be a float is still an int, so the check above admits it and
        # the conversion is the first thing that can tell. This function's whole job is to turn a
        # bad timeout into a typed refusal, and without this it was the one input class that could
        # crash it instead -- from *before* either `request` method's `try`, so the boundary
        # backstop never saw it.
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "timeout") from exc
    if not isfinite(seconds) or not 0 < seconds <= MAX_TIMEOUT_SECONDS:
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "timeout")
    return seconds


_STATUS_OUTCOMES: Mapping[int, PromptModelOutcomeId] = {
    401: PromptModelOutcomeId.AUTHENTICATION,
    403: PromptModelOutcomeId.AUTHENTICATION,
    404: PromptModelOutcomeId.MODEL_MISSING,
    413: PromptModelOutcomeId.REQUEST_TOO_LARGE,
    415: PromptModelOutcomeId.UNSUPPORTED_MEDIA,
    429: PromptModelOutcomeId.RATE_LIMITED,
}


#: How deeply an untrusted JSON document may nest before it is refused. Every shape this
#: repository actually receives is shallow -- an Ollama tags listing is four levels, a show
#: response five -- so this bound is generous by an order of magnitude and still far below the
#: interpreter's recursion limit.
MAX_JSON_NESTING_DEPTH = 64


def _refuse_deep_nesting(text: str) -> None:
    """Refuse a document whose nesting would recurse the parser, before handing it to the parser.

    `json.loads` is recursive, and on a deeply nested document it raises `RecursionError` -- a
    `RuntimeError`, not a `ValueError` -- which slips past every `JSONDecodeError` handler and
    escapes the typed-outcome contract entirely. A few hundred bytes of `[[[[...` is enough, well
    inside the response-size cap, so the size bound does not imply this one.

    Catching `RecursionError` afterwards would also work, but it would be answering the question
    "did we nearly exhaust the stack?" instead of "is this document shaped like something we
    accept?". Refusing first keeps the promise the plan actually makes: bounded depth.

    Brackets inside string literals are not structure. Skipping them matters: a legitimate answer
    may contain any amount of `[` in its prose, and counting those would refuse valid documents.
    """

    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise PromptModelTransportError(
                    PromptModelOutcomeId.MALFORMED_RESPONSE, "json_depth"
                )
        elif character in "]}":
            depth -= 1


def decode_bounded_json(raw: bytes) -> object:
    """Decode one untrusted JSON document with its depth bounded first.

    The single crossing point from provider bytes to Python values. `RecursionError` is still
    caught as a backstop: the depth check makes it unreachable, and an unreachable guard that
    costs nothing is worth keeping when the failure it prevents is an untyped crash.
    """

    text = raw.decode("utf-8")
    _refuse_deep_nesting(text)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                # SECURITY: duplicate keys let a parser and a reviewer see different policy or
                # usage values. Refuse ambiguity at the single provider-JSON crossing point.
                raise PromptModelTransportError(
                    PromptModelOutcomeId.MALFORMED_RESPONSE, "json_duplicate_key"
                )
            value[key] = item
        return value

    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except RecursionError as exc:  # pragma: no cover - unreachable behind the depth bound
        raise PromptModelTransportError(
            PromptModelOutcomeId.MALFORMED_RESPONSE, "json_depth"
        ) from exc


class LoopbackJsonExchange:
    """One bounded JSON client for both local families, built from an admitted destination."""

    def __init__(
        self,
        destination: AdmittedDestination,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_PROMPT_MODEL_RESPONSE_BYTES,
        address_resolver: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(destination, AdmittedDestination):
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "destination")
        allowed = FAMILY_PATHS.get(destination.family)
        if allowed is None:
            raise PromptModelTransportError(PromptModelOutcomeId.CAPABILITY_MISMATCH, "family")
        if not destination.loopback:
            # Unreachable for an admitted local family; kept because this class is the last thing
            # standing between a destination and a socket.
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "loopback")
        if (
            type(max_response_bytes) is not int
            or not 0 < max_response_bytes <= MAX_PROMPT_MODEL_RESPONSE_BYTES
        ):
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "response_limit")
        self._destination = destination
        self._allowed_paths = allowed
        self._timeout_seconds = _timeout(timeout_seconds, 30.0)
        self._max_response_bytes = max_response_bytes
        self._resolver = address_resolver

    @property
    def destination(self) -> AdmittedDestination:
        return self._destination

    @property
    def allowed_paths(self) -> frozenset[str]:
        return self._allowed_paths

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        if method not in {"GET", "POST"} or path not in self._allowed_paths:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "method_or_path")
        request_timeout = min(
            self._timeout_seconds, _timeout(timeout_seconds, self._timeout_seconds)
        )

        body: bytes | None = None
        if payload is not None:
            if not isinstance(payload, Mapping):
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "payload")
            try:
                body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                # M22-23. `json.dumps` recurses, so a deeply nested payload raises RecursionError,
                # which is neither TypeError nor ValueError and escaped this block untyped -- and
                # from before the `try`, so the boundary backstop never saw it either. The mirror
                # of `_refuse_deep_nesting`, which exists for exactly this on the decode side.
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "payload") from exc
            if len(body) > MAX_PROMPT_MODEL_REQUEST_BYTES:
                raise PromptModelTransportError(PromptModelOutcomeId.REQUEST_TOO_LARGE, "payload")

        connection: http.client.HTTPConnection | None = None
        unexpected: str | None = None
        try:
            connection = http.client.HTTPConnection(
                self._destination.host, self._destination.port, timeout=request_timeout
            )
            if self._resolver is not None:
                pinned = self._resolver(self._destination.host)
                # CRITICAL: keep the admitted hostname as the HTTP authority while the socket is
                # pinned to the already-validated address; reconnecting by name would reopen DNS.
                connection._create_connection = (  # type: ignore[attr-defined]
                    lambda address, connect_timeout, source: socket.create_connection(
                        (pinned, address[1]), connect_timeout, source
                    )
                )
            connection.request(
                method,
                path,
                body=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(self._max_response_bytes + 1)
            if len(raw) > self._max_response_bytes:
                raise PromptModelTransportError(
                    PromptModelOutcomeId.MALFORMED_RESPONSE, "response_size"
                )
            if response.status != 200:
                mapped = _STATUS_OUTCOMES.get(response.status, PromptModelOutcomeId.PROVIDER_ERROR)
                raise PromptModelTransportError(mapped, raw.decode("utf-8", errors="replace")[:512])
            try:
                value = decode_bounded_json(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PromptModelTransportError(
                    PromptModelOutcomeId.MALFORMED_RESPONSE, "json"
                ) from exc
            if not isinstance(value, Mapping):
                raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "shape")
            return value
        except PromptModelTransportError:
            raise
        except PromptModelContractError:
            # M22-23. Forward-looking rather than live: nothing this body currently calls raises a
            # contract error -- the remote twin's route is `scrub_upstream_error`, which has no
            # equivalent here. It is present so that the two transports answer the same question
            # the same way, and so that adding a core call inside this `try` cannot silently start
            # reporting a broken invariant of ours as a provider failure.
            raise
        except TimeoutError as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, "") from exc
        except ConnectionError as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.BACKEND_ABSENT, "") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "") from exc
        except Exception as exc:
            # M22-23. This family carries no credential, so the disclosure hazard that shapes the
            # remote twin does not apply here -- but an untyped escape is still an untyped escape,
            # and this is the family an ordinary user reaches first. The same form is used so the
            # two transports cannot drift into different answers for the same question.
            unexpected = type(exc).__name__
        finally:
            if connection is not None:
                # Same reasoning as the remote twin: a failure to close a socket being abandoned
                # cannot be acted on, and raising in a `finally` would replace the outcome in
                # flight instead of adding to it.
                try:
                    connection.close()
                except Exception:  # noqa: S110 - swallowing is the fix, not an oversight
                    pass
        raise PromptModelTransportError(
            PromptModelOutcomeId.TRANSPORT, "unexpected", unexpected_type=unexpected
        )


def resolve_exact_identity(model_id: str, exchange: object) -> tuple[ExactTagsRow, ShowIdentity]:
    """Ask the local runtime, strictly, what it is holding under one exact name (M22-13).

    Two requests in a fixed order and no retry: tags first, because a name that is not present or
    is present twice must stop the check before a second request is spent on it, then show for the
    one row that survived. `verbose` is explicitly false -- the verbose form returns the full tensor
    table, which is a large amount of provider data this lane has no use for and would rather never
    receive.
    """

    listing = exchange.request("GET", "/api/tags")  # type: ignore[attr-defined]
    tags = parse_exact_tags_row(listing, model_id)
    return tags, resolve_show_identity(model_id, exchange)


def resolve_prechat_identity(
    profile: PromptModelProfile, exchange: object, model: ModelChoice | None = None
) -> LiveModelIdentity | None:
    """The identity check made immediately before one generation request (M22-13).

    A qualified local Ollama profile is held to the strict digest-bearing tags reading: one exact
    row, no duplicate of the selected id, and a listing that is refused whole if any row is
    malformed. OpenAI-compatible providers publish alias identity without the local digest shape,
    so qualified and catalog-only remote profiles both use their exact catalog discovery route and
    retain the weaker, explicit alias-verification basis.
    """

    if isinstance(profile, LegacyPromptModelProfile):
        model_id = profile.model_id
    elif isinstance(model, ModelChoice) and model.profile_id == profile.profile_id:
        model_id = model.model_id
    else:
        raise PromptModelContractError("identity_model")
    if not isinstance(profile, LegacyPromptModelProfile) and profile.family in REMOTE_FAMILIES:
        route = profile.discovery_routes[0]
        listing = exchange.request("GET", route)  # type: ignore[attr-defined]
        rows = read_native_models(profile.family, listing, route)
        matches = tuple(facts for identifier, facts in rows if identifier == model_id)
        if len(matches) > 1:
            raise PromptModelContractError("identity_duplicate")
        return None if not matches else LiveModelIdentity(model_id, None, matches[0])
    if profile.family is not PromptModelFamily.OLLAMA or (
        isinstance(profile, LegacyPromptModelProfile)
        and profile.qualification_state is not PromptModelQualificationState.QUALIFIED
    ):
        return resolve_live_identity(
            profile.family,
            model_id,
            exchange,
            discovery_route=(profile.discovery_routes[0] if profile.discovery_routes else None),
        )
    listing = exchange.request("GET", "/api/tags")  # type: ignore[attr-defined]
    row = parse_exact_tags_row(listing, model_id)
    return LiveModelIdentity(model_id=row.model_id, digest=row.model_digest)


def resolve_show_identity(model_id: str, exchange: object) -> ShowIdentity:
    """The show half on its own, for the caller that already holds a tags listing.

    An explicit readiness check costs tags plus show and no more, so the probe -- which fetched
    the listing to build its census -- reuses that response and spends its second request here
    rather than asking for the listing again.
    """

    detail = exchange.request(  # type: ignore[attr-defined]
        "POST", "/api/show", {"model": model_id, "verbose": False}
    )
    return parse_show_identity(detail)


# M22-17. Google's OpenAI-compatibility surface spells one model two ways: the chat route takes the
# bare id, while the listing route answers with the native Gemini resource name, `models/<id>`.
# Comparing the request spelling against the catalog spelling reported a published model as missing
# and ended the session before it sent a single request byte. The two domains are joined here, at
# the single point where a listing row is first read -- the same place, and for the same reason, as
# the M22-13 digest join below. The map is closed: a route with no entry keeps exact equality.
_CATALOG_IDENTIFIER_PREFIXES: Mapping[str, str] = {
    "/v1beta/openai/models": "models/",
    "/v1beta/models?pageSize=1000": "models/",
}


def request_spelling(identifier: str, discovery_route: str | None) -> str:
    """A catalog row's identifier in the spelling the profile pins the model by.

    Both readers of a listing need this: the identity check compares a row against the pinned id,
    and the readiness census judges a row against the pinned set. They must agree, so they share one
    join rather than each carrying its own idea of what the provider answered.
    """

    prefix = None if discovery_route is None else _CATALOG_IDENTIFIER_PREFIXES.get(discovery_route)
    if prefix is None or not identifier.startswith(prefix):
        return identifier
    return identifier[len(prefix) :]


def resolve_live_identity(
    family: PromptModelFamily,
    model_id: str,
    exchange: object,
    *,
    discovery_route: str | None = None,
) -> LiveModelIdentity | None:
    """Ask the runtime what it is holding right now. `None` means the runtime does not have it."""

    if family is PromptModelFamily.OLLAMA:
        listing = exchange.request("GET", "/api/tags")  # type: ignore[attr-defined]
        models = listing.get("models")
        if not isinstance(models, list):
            raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "tags")
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("name") == model_id or entry.get("model") == model_id:
                # M22-13. Ollama reports `digest` as bare lowercase hex while every digest this
                # repository stores or compares is spelled `sha256:<hex>`. Handing the wire form
                # onward made a correct host fail its own digest check, so the domains are joined
                # here, at the single point where the wire value is first read.
                raw = entry.get("digest")
                digest: str | None = None
                if isinstance(raw, str) and raw:
                    try:
                        digest = normalize_ollama_digest(raw)
                    except PromptModelContractError as exc:
                        raise PromptModelTransportError(
                            PromptModelOutcomeId.MALFORMED_RESPONSE, "digest"
                        ) from exc
                return LiveModelIdentity(model_id=model_id, digest=digest)
        return None
    if family in OPENAI_COMPATIBLE_FAMILIES or family is PromptModelFamily.REMOTE_ANTHROPIC:
        route = "/v1/models" if discovery_route is None else discovery_route
        listing = exchange.request("GET", route)  # type: ignore[attr-defined]
        data = listing.get("data")
        if not isinstance(data, list):
            raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "models")
        if family is PromptModelFamily.REMOTE_ANTHROPIC:
            # M22-19. One provider listing, one row ceiling, and this branch reads the same
            # listing the census reads next. A private 256-row constant lived here and, being
            # the lower of the two, silently decided -- keeping this family refused at exactly
            # the hosted-catalogue sizes this item exists to admit.
            if len(data) > MAX_DISCOVERY_ROWS or any(
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("id"), str)
                or not entry.get("id")
                for entry in data
            ):
                raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "models")
            matches = tuple(entry for entry in data if entry.get("id") == model_id)
            if len(matches) > 1:
                # SECURITY: qualification needs one exact identity, not the first row from an
                # ambiguous provider listing. Duplicate authority fails closed.
                raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "models")
            if matches:
                return LiveModelIdentity(model_id=model_id, digest=None)
            return None
        # A row this branch cannot read is skipped rather than refusing the whole listing, which is
        # weaker than the census and the Anthropic branch. It is safe here and deliberately so: a
        # match returns the caller's own `model_id`, never a string taken from the provider, so an
        # unreadable row can only ever cost a match it was never going to win.
        matches = tuple(
            entry
            for entry in data
            if isinstance(entry, Mapping)
            and isinstance(entry.get("id"), str)
            and request_spelling(entry["id"], route) == model_id
        )
        if len(matches) > 1:
            # SECURITY: qualification needs one exact identity, not the first row from an ambiguous
            # provider listing. Two spellings of one model resolving together fails closed.
            raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "models")
        if matches:
            # Remote model catalogs publish no weight digest, so the receipt records the
            # weaker exact-id basis rather than claim a verification that cannot happen here.
            return LiveModelIdentity(model_id=model_id, digest=None)
        return None
    raise PromptModelTransportError(PromptModelOutcomeId.CAPABILITY_MISMATCH, "family")


@dataclass(frozen=True, slots=True)
class PromptModelSessionResult:
    """Every session yields an outcome; only a completed one also yields an answer."""

    outcome: PromptModelOutcome
    answer: PromptModelAnswer | None

    @property
    def completed(self) -> bool:
        return self.answer is not None


def _failure(outcome_id: PromptModelOutcomeId, detail: str) -> PromptModelSessionResult:
    # Keep session and readiness failures on the same canonical remediation table.
    remediation = remediation_for(outcome_id)
    return PromptModelSessionResult(
        outcome=build_prompt_model_outcome(
            outcome_id,
            severity=ValidationSeverity.ERROR,
            remediation=remediation,
            parameters=(),
            provider_detail=UntrustedProviderDetail.from_provider_text(detail) if detail else None,
        ),
        answer=None,
    )


ACTION_DEADLINE_SECONDS = 120.0
MAX_ACTION_TRANSMISSIONS = 4


@dataclass(slots=True)
class PromptModelActionState:
    """Private action budget, one identity, and a session-owned compatibility preference."""

    clock: Callable[[], float] = time.monotonic
    safe_preferences: set[tuple[str, str]] = field(default_factory=set)
    started: float = field(init=False)
    transmissions: int = 0
    downgraded: bool = False
    observed_model_id: str = ""
    identity: LiveModelIdentity | None = None
    identity_key: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        self.started = self.clock()


class _ActionExchange:
    def __init__(
        self,
        inner: object,
        state: PromptModelActionState,
        cancellation: Callable[[], bool] | None,
        timeout_seconds: float,
    ) -> None:
        self._inner = inner
        self._state = state
        self._cancellation = cancellation
        self._timeout_seconds = timeout_seconds

    def check(self) -> None:
        if self._cancellation is not None and self._cancellation():
            raise PromptModelTransportError(PromptModelOutcomeId.CANCELLED)
        if self._state.clock() - self._state.started >= ACTION_DEADLINE_SECONDS:
            raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        self.check()
        if self._state.transmissions >= MAX_ACTION_TRANSMISSIONS:
            raise PromptModelTransportError(PromptModelOutcomeId.REQUEST_TOO_LARGE)
        remaining = ACTION_DEADLINE_SECONDS - (self._state.clock() - self._state.started)
        if remaining <= 0:
            raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT)
        timeout = min(self._timeout_seconds, remaining)
        if timeout_seconds is not None:
            timeout = min(timeout, timeout_seconds)
        # CRITICAL: identity, downgrade and repair spend the same conservative budget. A failed
        # send may have left the process, so increment before delegation, never after success.
        self._state.transmissions += 1
        response = self._inner.request(method, path, payload, timeout_seconds=timeout)  # type: ignore[attr-defined]
        self.check()
        return cast(Mapping[str, object], response)


def run_prompt_model_session(
    request: PromptModelSessionRequest,
    exchange: object,
    *,
    cancellation: Callable[[], bool] | None = None,
    timeout_seconds: float | None = None,
    action_state: PromptModelActionState | None = None,
    options: RequestOptions | None = None,
) -> PromptModelSessionResult:
    """Verify identity, admit, send, parse. Any refusal returns before a request is issued."""

    if not isinstance(request, PromptModelSessionRequest):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "request")
    if cancellation is not None and not callable(cancellation):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "cancellation")
    if action_state is None:
        action_state = PromptModelActionState()
    if not isinstance(action_state, PromptModelActionState):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "action_state")
    key = (request.profile.profile_id, request.model_id)
    chosen_options = RequestOptions() if options is None else options
    if not isinstance(chosen_options, RequestOptions):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "request_options")
    if key in action_state.safe_preferences:
        chosen_options = RequestOptions.safe()
    chosen_options = options_for_metadata(request, chosen_options)
    if request.model is not None:
        decision = chosen_model_budget(request, chosen_options)
        if decision.plan is None:
            return PromptModelSessionResult(outcome=decision.outcome, answer=None)
        request = replace(request, plan=decision.plan)
    exchange = _ActionExchange(
        exchange,
        action_state,
        cancellation,
        _timeout(timeout_seconds, request.profile.request_timeout_seconds),
    )

    admission = admit_session_request(request)
    if admission.outcome_id is not PromptModelOutcomeId.OK:
        return PromptModelSessionResult(outcome=admission, answer=None)
    if cancellation is not None and cancellation():
        return _failure(PromptModelOutcomeId.CANCELLED, "")

    try:
        exchange.check()
        if action_state.identity_key not in (None, key):
            raise PromptModelTransportError(PromptModelOutcomeId.CANCELLED)
        observed = action_state.identity
        if observed is None:
            observed = resolve_prechat_identity(request.profile, exchange, request.model)
    except PromptModelTransportError as exc:
        return _failure(exc.outcome_id, "")
    except PromptModelContractError as exc:
        # The strict reading refused the listing. The detail is this repository's own closed code,
        # never provider prose, so it is safe to carry into the receipt and useful there.
        return _failure(PromptModelOutcomeId.MALFORMED_RESPONSE, str(exc))
    if observed is None:
        return _failure(PromptModelOutcomeId.MODEL_MISSING, "")

    identity = verify_live_identity(request.profile, observed, request.model)
    if identity.verification is None:
        return PromptModelSessionResult(outcome=identity.outcome, answer=None)
    action_state.identity = observed
    action_state.identity_key = key
    if request.model is not None and observed.metadata is not None:
        request = replace(request, model=replace(request.model, metadata=observed.metadata))
        chosen_options = options_for_metadata(request, chosen_options)
        decision = chosen_model_budget(request, chosen_options)
        if decision.plan is None:
            return PromptModelSessionResult(outcome=decision.outcome, answer=None)
        request = replace(request, plan=decision.plan)
    if cancellation is not None and cancellation():
        return _failure(PromptModelOutcomeId.CANCELLED, "")

    if request.family is PromptModelFamily.OLLAMA:
        path = "/api/chat"
    elif request.family is PromptModelFamily.LOOPBACK_SERVER:
        path = request.profile.chat_route or "/v1/chat/completions"
    else:
        path = request.profile.chat_route
    # Built before the try: a payload this build refuses to assemble is a contract violation on
    # our side, not a malformed answer from the provider, and must not be reported as one.
    payload = build_request_payload(request, chosen_options)
    if (
        len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        > request.profile.capabilities.max_request_bytes
    ):
        return _failure(PromptModelOutcomeId.REQUEST_TOO_LARGE, "")
    try:
        response = exchange.request("POST", path, payload, timeout_seconds=timeout_seconds)
    except PromptModelTransportError as exc:
        safe_payload = build_request_payload(request, RequestOptions.safe())
        expected_error_kind = ""
        if request.family in REMOTE_FAMILIES:
            expected_error_kind = (
                "INVALID_ARGUMENT"
                if policy_for_profile(request.profile).provider_id == "google_gemini"
                else "invalid_request_error"
            )
        if (
            exc.http_status != 400
            or not expected_error_kind
            or exc.error_kind != expected_error_kind
            or request.family not in REMOTE_FAMILIES
            or payload == safe_payload
            or action_state.downgraded
        ):
            return _failure(exc.outcome_id, "")
        # SECURITY: only a closed invalid-400 type authorizes one retry; never inspect message
        # prose for option names, and never reset this allowance for the repair round.
        try:
            exchange.check()
        except PromptModelTransportError as cancelled:
            return _failure(cancelled.outcome_id, "")
        action_state.downgraded = True
        action_state.safe_preferences.add(key)
        if request.model is not None:
            # IMPORTANT: dropping reasoning controls changes the required output headroom.
            # Re-admit the same measured text before retrying; never reset the action budget.
            decision = chosen_model_budget(
                request, options_for_metadata(request, RequestOptions.safe())
            )
            if decision.plan is None:
                return PromptModelSessionResult(outcome=decision.outcome, answer=None)
            request = replace(request, plan=decision.plan)
            safe_payload = build_request_payload(request, RequestOptions.safe())
            if (
                len(
                    json.dumps(safe_payload, ensure_ascii=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
                > request.capabilities.max_request_bytes
            ):
                return _failure(PromptModelOutcomeId.REQUEST_TOO_LARGE, "")
        try:
            response = exchange.request("POST", path, safe_payload, timeout_seconds=timeout_seconds)
        except PromptModelTransportError as retry_error:
            return _failure(retry_error.outcome_id, "")
        except PromptModelContractError as retry_error:
            return _failure(PromptModelOutcomeId.MALFORMED_RESPONSE, str(retry_error))
    except PromptModelContractError as exc:
        # M22-23. The identity leg above has always had this; the chat leg did not, so a contract
        # error raised inside `request` -- which the transport now deliberately lets through rather
        # than retyping -- would have left this function uncaught. Same closed code, same handling.
        return _failure(PromptModelOutcomeId.MALFORMED_RESPONSE, str(exc))
    try:
        decoded = read_response_answer(request.family, response, request.model_id)
    except DialectResponseError as exc:
        return _failure(exc.outcome_id, "")
    except PromptModelContractError as exc:
        return _failure(PromptModelOutcomeId.MALFORMED_RESPONSE, str(exc))
    action_state.observed_model_id = decoded.observed_model_id
    try:
        exchange.check()
    except PromptModelTransportError as exc:
        return _failure(exc.outcome_id, "")
    return PromptModelSessionResult(
        outcome=build_prompt_model_outcome(
            PromptModelOutcomeId.OK,
            severity=ValidationSeverity.INFO,
            remediation=PromptModelRemediation.NONE,
            parameters=(("verification", identity.verification.value),),
        ),
        answer=PromptModelAnswer(
            text=decoded.text,
            verification=identity.verification,
            finish_reason=decoded.finish_reason,
            observed_model_id=decoded.observed_model_id,
        ),
    )


def _probe_environment() -> dict[str, str]:
    """A minimal environment: enough to start an interpreter, nothing that could carry a secret."""

    keys = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LD_LIBRARY_PATH")
    return {key: os.environ[key] for key in keys if key in os.environ}


def probe_native_runtime(module_name: str, *, timeout_seconds: float = 15.0) -> NativeProbeReport:
    """Ask a separate interpreter what a native runtime can do. Every failure blocks the family."""

    if module_name not in PROBEABLE_RUNTIMES:
        return NativeProbeReport(
            status=NativeProbeStatus.UNAVAILABLE, runtime_version="", detected_backends=()
        )
    if (
        type(timeout_seconds) not in {int, float}
        or not isfinite(float(timeout_seconds))
        or not 0 < float(timeout_seconds) <= MAX_PROBE_TIMEOUT_SECONDS
    ):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "probe_timeout")

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, isolated interpreter, no shell
            [sys.executable, "-I", "-S", "-c", _PROBE_SOURCE, module_name],
            capture_output=True,
            timeout=float(timeout_seconds),
            check=False,
            env=_probe_environment(),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return NativeProbeReport(
            status=NativeProbeStatus.TIMED_OUT, runtime_version="", detected_backends=()
        )
    except OSError:
        return NativeProbeReport(
            status=NativeProbeStatus.UNAVAILABLE, runtime_version="", detected_backends=()
        )

    if completed.returncode != 0:
        return NativeProbeReport(
            status=NativeProbeStatus.CRASHED, runtime_version="", detected_backends=()
        )
    raw = completed.stdout[: MAX_PROBE_OUTPUT_BYTES + 1]
    if len(raw) > MAX_PROBE_OUTPUT_BYTES:
        return NativeProbeReport(
            status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
        )
    return decode_probe_output(raw)


def decode_probe_output(raw: bytes) -> NativeProbeReport:
    """Validate the closed JSON shape. Anything else is malformed, never partially trusted."""

    try:
        value = decode_bounded_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, PromptModelTransportError):
        return NativeProbeReport(
            status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
        )
    if not isinstance(value, Mapping) or set(value) != {"runtime_version", "detected_backends"}:
        return NativeProbeReport(
            status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
        )
    version = value["runtime_version"]
    backends = value["detected_backends"]
    if not isinstance(version, str) or len(version) > 64:
        return NativeProbeReport(
            status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
        )
    if not isinstance(backends, list) or len(backends) > 8:
        return NativeProbeReport(
            status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
        )
    for item in backends:
        if not isinstance(item, str) or not item or len(item) > 32:
            return NativeProbeReport(
                status=NativeProbeStatus.MALFORMED, runtime_version="", detected_backends=()
            )
    return NativeProbeReport(
        status=NativeProbeStatus.READY,
        runtime_version=version,
        detected_backends=tuple(backends),
    )


# --- M22-04 consented remote family -------------------------------------------------------------
#
# The remote path is deliberately a separate class from LoopbackJsonExchange. They share almost no
# policy: one is plaintext to an address that cannot leave the machine, the other is TLS to a third
# party under an explicit, revocable consent with a credential attached. Folding them into one
# client with flags is how a loopback assumption ends up applied to an internet destination.

REMOTE_PROMPT_PATHS = frozenset(
    route
    for policy in remote_provider_policies()
    for route in (policy.discovery_route, policy.chat_route)
)
MAX_REMOTE_RESPONSE_BYTES = 2_000_000


def _embedded_ipv4(literal: IPv6Address) -> IPv4Address | None:
    """Extract the IPv4 address an IPv6 literal carries, in either embedding form.

    `is_global` reads the deprecated `::a.b.c.d` form as an ordinary global address, because the
    high bits are zero rather than in any reserved block: `::7f00:1` and `::a9fe:a9fe` both report
    `is_global`. `.ipv4_mapped` only recognises the modern `::ffff:a.b.c.d` form, so the deprecated
    one has to be unpacked by hand. Both are returned here so the caller can judge the address that
    would actually be reached rather than the wrapper it arrived in.
    """

    mapped = literal.ipv4_mapped
    if mapped is not None:
        return mapped
    packed = literal.packed
    zero_prefix = bytes(12)
    if packed[:12] == zero_prefix and packed[12:] not in {bytes(4), bytes(3) + bytes([1])}:
        # `::` and `::1` are the unspecified and loopback addresses, not embeddings; both are
        # already refused by `is_global`, and reading them as IPv4 would be wrong.
        return IPv4Address(packed[12:])
    return None


def _address_is_admissible(literal: IPv4Address | IPv6Address) -> bool:
    """One address is admissible only if every address it can reach is globally routable."""

    if not literal.is_global or literal.is_multicast:
        return False
    if isinstance(literal, IPv6Address):
        embedded = _embedded_ipv4(literal)
        if embedded is not None and (not embedded.is_global or embedded.is_multicast):
            return False
    return True


def resolve_pinned_address_bounded(
    host: str,
    *,
    timeout_seconds: float,
    loopback_required: bool,
) -> str:
    """Resolve and policy-check one address under a killable process deadline.

    Socket timeouts begin only after name resolution. Running the fixed resolver in an isolated
    child makes the readiness deadline real even when the platform resolver stalls: Python can
    terminate the process, whereas it cannot cancel a thread blocked in ``getaddrinfo``.
    """

    if not isinstance(host, str) or not host or type(loopback_required) is not bool:
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "resolver_input")
    timeout = _timeout(timeout_seconds, 1.0)
    try:
        literal = ip_address(host)
    except ValueError:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed code/argv, isolated, no shell
                [sys.executable, "-I", "-S", "-c", _RESOLUTION_SOURCE, host],
                capture_output=True,
                timeout=timeout,
                check=False,
                env=_probe_environment(),
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, "") from exc
        except OSError as exc:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from exc
        if completed.returncode != 0:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
        raw = completed.stdout[: MAX_RESOLUTION_OUTPUT_BYTES + 1]
        if len(raw) > MAX_RESOLUTION_OUTPUT_BYTES:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
        try:
            decoded = decode_bounded_json(raw)
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from exc
        if not isinstance(decoded, Mapping) or set(decoded) != {"addresses"}:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
        addresses = decoded["addresses"]
        if (
            not isinstance(addresses, list)
            or not addresses
            or len(addresses) > MAX_RESOLUTION_ADDRESSES
            or any(not isinstance(item, str) or not item for item in addresses)
        ):
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
    else:
        addresses = [str(literal)]

    pinned = ""
    for candidate in addresses:
        try:
            literal = ip_address(candidate)
        except ValueError:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
        admissible = literal.is_loopback if loopback_required else _address_is_admissible(literal)
        if not admissible:
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "")
        if not pinned:
            pinned = str(literal)
    return pinned


def _resolve_pinned_address(
    host: str, *, getaddrinfo: Callable[..., list[Any]] = socket.getaddrinfo
) -> str:
    """Resolve a hostname once and refuse anything that is not globally routable.

    Validating a name and then connecting by name leaves a window in which the name resolves to
    something else. Every address the name currently resolves to is checked here, and the caller
    connects to the one address returned, so a later answer cannot redirect the socket.

    A single bad answer refuses the whole name rather than the one address, because a resolver that
    is answering with an internal address at all is not one whose other answers should be trusted.
    """

    try:
        infos = getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise PromptModelTransportError(PromptModelOutcomeId.DESTINATION_UNRESOLVED, "") from exc
    addresses: list[str] = []
    for info in infos:
        candidate = info[4][0]
        try:
            literal = ip_address(candidate)
        except ValueError:
            raise PromptModelTransportError(
                PromptModelOutcomeId.DESTINATION_UNRESOLVED, ""
            ) from None
        if not _address_is_admissible(literal):
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "")
        addresses.append(candidate)
    if not addresses:
        raise PromptModelTransportError(PromptModelOutcomeId.DESTINATION_UNRESOLVED, "")
    return addresses[0]


@dataclass(frozen=True, slots=True)
class RemoteExchangeMetrics:
    """Byte and status counts from the last call, for a content-free receipt."""

    http_status: int
    request_bytes: int
    response_bytes: int
    prompt_tokens: int
    completion_tokens: int
    usage_present: bool = False


class RemoteHttpsExchange:
    """TLS client for the consented remote family, pinned to a validated address."""

    def __init__(
        self,
        destination: AdmittedDestination,
        credential: RuntimeCredential,
        *,
        policy: RemoteProviderPolicy | None = None,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = MAX_REMOTE_RESPONSE_BYTES,
        address_resolver: Callable[[str], str] | None = None,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(destination, AdmittedDestination):
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "destination")
        if destination.family not in REMOTE_FAMILIES:
            raise PromptModelTransportError(PromptModelOutcomeId.CAPABILITY_MISMATCH, "family")
        if destination.scheme != "https" or destination.loopback:
            # The remote family is TLS to a third party. A plaintext or loopback destination here
            # would mean the consent the user gave does not describe what is about to happen.
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "scheme")
        if not isinstance(credential, RuntimeCredential):
            raise PromptModelTransportError(PromptModelOutcomeId.AUTHENTICATION, "credential")
        if not isinstance(policy, RemoteProviderPolicy):
            raise PromptModelTransportError(PromptModelOutcomeId.CAPABILITY_MISMATCH, "policy")
        policy_family = policy.family
        if destination.family is not policy_family:
            raise PromptModelTransportError(PromptModelOutcomeId.CAPABILITY_MISMATCH, "family")
        expected_destination = admit_egress_destination(policy_family, policy.origin)
        if destination != expected_destination:
            # SECURITY: a guarded public destination is not enough. Consent names one provider,
            # and its origin/routes/headers travel as one indivisible policy value.
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "policy_origin")
        if (
            type(max_response_bytes) is not int
            or not 0 < max_response_bytes <= MAX_REMOTE_RESPONSE_BYTES
        ):
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "response_limit")
        self._destination = destination
        self._credential = credential
        self._policy = policy
        self._allowed_paths = frozenset({policy.discovery_route, policy.chat_route})
        self._timeout_seconds = _timeout(timeout_seconds, 60.0)
        self._max_response_bytes = max_response_bytes
        self._resolver = address_resolver
        self._connection_factory = connection_factory
        self._tls_context: ssl.SSLContext | None = None
        self._metrics: RemoteExchangeMetrics | None = None
        self._transmission_count = 0

    @property
    def destination(self) -> AdmittedDestination:
        return self._destination

    @property
    def metrics(self) -> RemoteExchangeMetrics | None:
        return self._metrics

    @property
    def transmission_count(self) -> int:
        return self._transmission_count

    def _connect(self, timeout: float) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(
                self._destination.host, self._destination.port, timeout=timeout
            )
        if self._resolver is None:
            # CRITICAL: socket timeouts do not bound platform DNS. The production path must use
            # the killable resolver process; the injectable resolver exists only for tests.
            pinned = resolve_pinned_address_bounded(
                self._destination.host,
                timeout_seconds=timeout,
                loopback_required=False,
            )
        else:
            pinned = self._resolver(self._destination.host)
        if self._tls_context is None:
            # Built once per exchange rather than once per call: a default context loads the OS
            # trust store, and rebuilding it per request buys nothing. It stays a *default* context,
            # which is what makes `CERT_REQUIRED` and `check_hostname` true rather than assumed.
            self._tls_context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            self._destination.host,
            self._destination.port,
            timeout=timeout,
            context=self._tls_context,
        )
        # Keep the hostname for SNI and certificate verification while the socket goes to the one
        # address that was validated a moment ago.
        connection._create_connection = (  # type: ignore[attr-defined]
            lambda address, connect_timeout, source: socket.create_connection(
                (pinned, address[1]), connect_timeout, source
            )
        )
        return connection

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        method_and_path = (method, path)
        if method_and_path not in {
            ("GET", self._policy.discovery_route),
            ("POST", self._policy.chat_route),
        }:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "method_or_path")
        if self._transmission_count >= self._policy.max_transmissions:
            # SECURITY: the exchange is one action's capability. Reusing it must not turn the
            # catalog's two-call discovery/chat ceiling into an unbounded credentialed client.
            raise PromptModelTransportError(PromptModelOutcomeId.REQUEST_TOO_LARGE, "call_limit")
        request_timeout = min(
            self._timeout_seconds, _timeout(timeout_seconds, self._timeout_seconds)
        )
        body: bytes | None = None
        if payload is not None:
            if not isinstance(payload, Mapping):
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "payload")
            try:
                body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                # M22-23. `json.dumps` recurses, so a deeply nested payload raises RecursionError,
                # which is neither TypeError nor ValueError and escaped this block untyped -- and
                # from before the `try`, so the boundary backstop never saw it either. The mirror
                # of `_refuse_deep_nesting`, which exists for exactly this on the decode side.
                raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "payload") from exc
            if len(body) > MAX_PROMPT_MODEL_REQUEST_BYTES:
                raise PromptModelTransportError(PromptModelOutcomeId.REQUEST_TOO_LARGE, "payload")

        connection: Any = None
        unexpected: str | None = None
        try:
            connection = self._connect(request_timeout)
            headers: dict[str, str] = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            scheme = self._policy.credential_for_route(method, path)
            if scheme is RemoteCredentialScheme.X_API_KEY:
                # CRITICAL: Anthropic API keys are not Bearer credentials. Construct the raw
                # x-api-key value only here, immediately before the socket receives the headers.
                headers["x-api-key"] = self._credential.api_key_header()
                if self._policy.api_version is None:  # pragma: no cover - policy validates this
                    raise PromptModelTransportError(
                        PromptModelOutcomeId.CAPABILITY_MISMATCH, "api_version"
                    )
                headers["anthropic-version"] = self._policy.api_version
            elif scheme is RemoteCredentialScheme.X_GOOG_API_KEY:
                # CRITICAL: the native Google listing key belongs only to its admitted GET;
                # adding it to compatible chat would expose two credential schemes at once.
                headers["x-goog-api-key"] = self._credential.api_key_header()
            else:
                headers["Authorization"] = self._credential.authorization_header()
            headers.update(dict(self._policy.extra_headers))
            # Count before handing bytes to the connection: an exception during request cannot
            # prove that nothing left the process, so the conservative budget is consumed.
            self._transmission_count += 1
            connection.request(
                method,
                path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read(self._max_response_bytes + 1)
            if len(raw) > self._max_response_bytes:
                raise PromptModelTransportError(
                    PromptModelOutcomeId.MALFORMED_RESPONSE, "response_size"
                )
            self._metrics = RemoteExchangeMetrics(
                http_status=response.status,
                request_bytes=len(body or b""),
                response_bytes=len(raw),
                prompt_tokens=0,
                completion_tokens=0,
                usage_present=False,
            )
            if 300 <= response.status <= 399:
                # A redirect would move the request off the destination the chokepoint admitted, so
                # it is refused and the Location header is never read.
                raise PromptModelTransportError(PromptModelOutcomeId.REDIRECT_REFUSED, "")
            decoded: object = None
            try:
                decoded = decode_bounded_json(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if response.status != 200:
                error = scrub_upstream_error(response.status, decoded)
                provider_error = decoded.get("error") if isinstance(decoded, Mapping) else None
                error_kind = ""
                if isinstance(provider_error, Mapping):
                    # IMPORTANT: Google's bound policy ID is google_gemini; its closed status
                    # field authorizes the one safe retry. Never classify from error prose.
                    kind = (
                        provider_error.get("status")
                        if self._policy.provider_id == "google_gemini"
                        else provider_error.get("type")
                    )
                    if isinstance(kind, str) and kind in {
                        "invalid_request_error",
                        "INVALID_ARGUMENT",
                    }:
                        error_kind = str(kind)
                raise PromptModelTransportError(
                    map_remote_outcome(error),
                    "",
                    http_status=response.status,
                    error_kind=error_kind,
                )
            if not isinstance(decoded, Mapping):
                raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "shape")
            usage = decoded.get("usage")
            prompt_tokens = 0
            completion_tokens = 0
            if usage is not None and not isinstance(usage, Mapping):
                raise PromptModelTransportError(PromptModelOutcomeId.MALFORMED_RESPONSE, "usage")
            if isinstance(usage, Mapping) and (
                self._destination.family is PromptModelFamily.REMOTE_ANTHROPIC
            ):
                raw_prompt = usage.get("input_tokens")
                raw_completion = usage.get("output_tokens")
                cache_creation = usage.get("cache_creation_input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                if (
                    type(raw_prompt) is not int
                    or raw_prompt < 0
                    or type(raw_completion) is not int
                    or raw_completion < 0
                    or type(cache_creation) is not int
                    or cache_creation < 0
                    or type(cache_read) is not int
                    or cache_read < 0
                ):
                    raise PromptModelTransportError(
                        PromptModelOutcomeId.MALFORMED_RESPONSE, "usage"
                    )
                prompt_tokens = raw_prompt + cache_creation + cache_read
                completion_tokens = raw_completion
            elif isinstance(usage, Mapping):
                raw_prompt = usage.get("prompt_tokens")
                raw_completion = usage.get("completion_tokens")
                if (
                    type(raw_prompt) is not int
                    or raw_prompt < 0
                    or type(raw_completion) is not int
                    or raw_completion < 0
                ):
                    raise PromptModelTransportError(
                        PromptModelOutcomeId.MALFORMED_RESPONSE, "usage"
                    )
                prompt_tokens = raw_prompt
                completion_tokens = raw_completion
            self._metrics = RemoteExchangeMetrics(
                http_status=response.status,
                request_bytes=len(body or b""),
                response_bytes=len(raw),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_present=isinstance(usage, Mapping),
            )
            return decoded
        except PromptModelTransportError:
            raise
        except PromptModelContractError:
            # M22-23. Not a provider failure. This is one of this repository's own invariants
            # breaking, and `run_prompt_model_session` already turns it into MALFORMED_RESPONSE
            # carrying its closed code. Letting the backstop below take it would report a bug in
            # here as a problem with the user's network, and hide the bug behind a remediation.
            raise
        except TimeoutError as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TIMEOUT, "") from exc
        except ssl.SSLError as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.EGRESS_REFUSED, "tls") from exc
        except ConnectionError as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "") from exc
        except Exception as exc:
            # SECURITY: recorded here and raised after the `try` statement, deliberately. Raising
            # inside this handler -- even `from None` -- leaves the original hanging off the new
            # error's `__context__`; `from None` only sets `__suppress_context__`, which stops the
            # default traceback printer from rendering the chain and drops no reference at all.
            # The object caught here is by definition one this build did not anticipate, next to a
            # live credential, and M22-22's instance held the entire `Authorization: Bearer <key>`
            # value in its `args`. Only the class name crosses, and it is an identifier fixed at
            # class-definition time, so it cannot carry provider content or a secret.
            unexpected = type(exc).__name__
        finally:
            # SECURITY: `authorization_header()` and `api_key_header()` return plain strings, so
            # while this method runs the raw credential sits in `headers` -- and a frame stays
            # alive on the `__traceback__` of anything raised here, where `f_locals` would hand it
            # to any reporter that captures frame locals. `RuntimeCredential` redacts itself, but
            # that protects the wrapper, not a header value already formatted from it. Clearing
            # the binding before the exception propagates removes it from the frame: `f_locals` on
            # a function frame is built from the fast locals when it is read, so an unbound name is
            # simply absent. Measured, not assumed, and pinned by a test.
            headers = {}
            if connection is not None:
                # A socket being abandoned has nothing actionable to report, and an exception
                # raised in a `finally` *replaces* the one in flight rather than adding to it, so
                # an unguarded close could discard the typed outcome the body just produced.
                # Ruff's suggestion to log it instead is the one thing that must not happen here:
                # this is the credential-carrying transport, and the object is of unknown content.
                try:
                    connection.close()
                except Exception:  # noqa: S110 - swallowing is the fix, not an oversight
                    pass
        raise PromptModelTransportError(
            PromptModelOutcomeId.TRANSPORT, "unexpected", unexpected_type=unexpected
        )


@dataclass(frozen=True, slots=True)
class RemoteSessionResult:
    """Outcome, answer when there is one, and the content-free receipt when a call was made."""

    outcome: PromptModelOutcome
    answer: PromptModelAnswer | None
    receipt: RemoteUsageReceipt | None

    @property
    def completed(self) -> bool:
        return self.answer is not None


class _GatedExchange:
    """Re-runs the consent gate before every request the session issues.

    One session issues more than one request: the identity check already carries the credential,
    and the chat call carries the prompt. Admitting once at session entry would mean a revocation
    made while the first call is in flight -- bounded only by the timeout -- does not stop the
    second, which is exactly the gap an explicit, revocable consent exists to close. The wrapper
    also means a future call added to the session inherits the gate instead of having to remember
    it.
    """

    __slots__ = ("_inner", "_gate")

    def __init__(self, inner: object, gate: Callable[[], PromptModelOutcome]) -> None:
        self._inner = inner
        self._gate = gate

    @property
    def metrics(self) -> object:
        return getattr(self._inner, "metrics", None)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        outcome = self._gate()
        if outcome.outcome_id is not PromptModelOutcomeId.OK:
            raise PromptModelTransportError(outcome.outcome_id, "")
        return self._inner.request(  # type: ignore[attr-defined,no-any-return]
            method, path, payload, timeout_seconds=timeout_seconds
        )


def run_remote_prompt_model_session(
    request: PromptModelSessionRequest,
    exchange: object,
    *,
    selected_profile_id: str,
    consent: object,
    credential: object,
    cancellation: Callable[[], bool] | None = None,
    timeout_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_date: date | None = None,
    action_state: PromptModelActionState | None = None,
    options: RequestOptions | None = None,
) -> RemoteSessionResult:
    """Check consent before every transmission, then run the session and receipt it.

    `consent` may be a `RemoteConsentRecord` or a zero-argument callable returning one. Pass the
    callable form -- `lambda: ledger.record_for(profile_id)` -- when a ledger is in use: it is what
    makes a revocation observable to a session that has already started. A bare record is re-read
    too, but a record cannot change, so only the callable form can report a withdrawal.
    """

    if not isinstance(request, PromptModelSessionRequest):
        raise PromptModelTransportError(PromptModelOutcomeId.TRANSPORT, "request")

    def read_consent() -> object:
        return consent() if callable(consent) else consent

    def decision_for(record: object) -> RemoteTransmissionDecision:
        return admit_remote_transmission(
            profile=request.profile,
            selected_profile_id=selected_profile_id,
            consent=record,
            credential=credential,
            carries_media=bool(request.image_payloads),
        )

    initial_consent = read_consent()
    decision = decision_for(initial_consent)
    if not decision.admitted:
        return RemoteSessionResult(outcome=decision.outcome, answer=None, receipt=None)
    policy = policy_for_profile(request.profile)

    def gate() -> PromptModelOutcome:
        # SECURITY: re-read network consent immediately before each request so a withdrawal
        # between identity discovery and drafting prevents the next transmission.
        return decision_for(read_consent()).outcome

    started = clock()
    result = run_prompt_model_session(
        request,
        _GatedExchange(exchange, gate),
        cancellation=cancellation,
        timeout_seconds=timeout_seconds,
        action_state=action_state,
        options=options,
    )
    elapsed_ms = max(0, int((clock() - started) * 1000))
    observed = getattr(exchange, "metrics", None)
    # Counts are read only from the declared type. A duck-typed object carrying the right attribute
    # names is not evidence of what was actually transferred, and a receipt that accepted it would
    # be reporting numbers nothing in this repository produced.
    counted = observed if isinstance(observed, RemoteExchangeMetrics) else None
    receipt = RemoteUsageReceipt(
        profile_id=request.profile.profile_id,
        host=request.destination.host,
        outcome_id=result.outcome.outcome_id,
        http_status=0 if counted is None else counted.http_status,
        request_bytes=0 if counted is None else counted.request_bytes,
        response_bytes=0 if counted is None else counted.response_bytes,
        prompt_tokens=0 if counted is None else counted.prompt_tokens,
        completion_tokens=0 if counted is None else counted.completion_tokens,
        duration_ms=elapsed_ms,
        # CRITICAL: a receipt records usage facts, never any fragment of runtime authorization.
        credential_last_four="",
        provider_id=policy.provider_id,
        model_id=request.model_id,
        policy_sha256=policy.fingerprint,
        usage_present=False if counted is None else counted.usage_present,
    )
    return RemoteSessionResult(outcome=result.outcome, answer=result.answer, receipt=receipt)
