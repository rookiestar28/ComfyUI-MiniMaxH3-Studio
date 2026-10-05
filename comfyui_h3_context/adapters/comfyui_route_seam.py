"""One HTTP edge for the owned JSON action routes.

Ten adapter modules register twelve routes, and ten of them each carried their own copy of the same
forty lines: the lazy host lookup, the already-owned scan, the content-type check, the origin check,
the byte budget, the streaming read that has to re-check the budget because `Content-Length` is a
claim rather than a fact, and the mapping from a typed refusal to a status. Copies drift, and by
M23-47 these had drifted in five measurable ways.

The one that matters is the origin check, which exists in four different forms across ten routes:

* seven refuse anything but exactly one owned origin;
* `comfyui_generation_profile` also admits an absent header, deliberately and correctly, because
  browsers omit `Origin` on a same-origin GET;
* `comfyui_sidebar_workspace`'s action route checks the header only *after* decoding the body and
  only when the decoded action is a proposal action, so every other action on a route that mutates
  workspace state was reachable cross-origin;
* `comfyui_duration_resolution` never read the header at all.

Nothing recorded a decision for either gap and no test would have noticed one being added or
removed, because the seven origin assertions are seven per-route tests rather than one invariant.
Both gaps are closed here, as a property of this seam. The other four drifts -- refusal body shape
(four of them), the unexpected-exception guard (present on five routes of ten), the malformed
`Content-Length` refusal (present on one), and the ownership attribute name (ten modules, ten
different names) -- are recorded below where each is handled.

CRITICAL: this module must not import `aiohttp` or `server`, at import time or ever. Both are
optional host modules; the package has to import cleanly without ComfyUI present, which is why the
lookup goes through `sys.modules` and returns `None` rather than raising when the host is absent.

Two routes are deliberately NOT served by this seam: `comfyui_media_preview` and
`comfyui_authoring_media_preview` drive request and work deadlines, refuse `Range` and query
strings, watch for a closing transport and answer with streamed or deliberately empty bodies. Their
edge is a different edge, and flattening it into this one would delete behaviour rather than share
it. Their admission is not different: every edge asks `origin_accepted` below.

Which origin is "owned" is decided per request (M23-57). It used to be the literal
`http://127.0.0.1:8188`, which refused the same page served at `localhost`, another port, IPv6
loopback, a directly served interface address or through a proxy. The rule, and why a trusted
alias never authorizes another, is in `core/request_target.py`; this module only gathers the
facts: the `Host` and `Origin` headers, the local endpoint of the socket that accepted the
connection, and the operator's `H3_CONTEXT_PUBLIC_ORIGINS`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from ..core.request_target import (
    PUBLIC_ORIGINS_VARIABLE,
    AdmissionReason,
    LocalEndpoint,
    OriginRequirement,
    PublicOrigins,
    PublicOriginsStatus,
    admission_refusal,
    local_endpoint,
    parse_public_origins,
)
from .route_workers import RouteWorkerCapacityError, run_route_work

_LOGGER = logging.getLogger(__name__)


class OriginRule(Enum):
    """How a route treats the `Origin` header, once the request's own target is known."""

    #: Exactly one origin, equal to the request's target. Anything else, including absence, is
    #: refused.
    EXACT = "exact"
    #: The request's target, or absent. Browsers omit the header on a same-origin GET, so a
    #: read-only route that refused an absent header would refuse its own page. A POST always
    #: carries one.
    EXACT_OR_ABSENT = "exact_or_absent"
    #: Native output navigations omit Origin but must not arrive from a foreign site.
    SAME_ORIGIN_GET = "same_origin_get"


_REQUIREMENTS = {
    OriginRule.EXACT: OriginRequirement.REQUIRED,
    OriginRule.EXACT_OR_ABSENT: OriginRequirement.OPTIONAL,
    OriginRule.SAME_ORIGIN_GET: OriginRequirement.OPTIONAL_SAME_ORIGIN_FETCH,
}


@dataclass(frozen=True, slots=True)
class RouteResult:
    """What a handler decided: a status, and a wire body when there is one.

    `wire is None` means a bodiless response, which is what six of the ten routes answer refusals
    with today. It does not mean "pick a body for me" -- this seam never invents one.
    """

    status: int
    wire: object | None = None
    headers: tuple[tuple[str, str], ...] = ()
    _encoded_json: str | None = None

    def __post_init__(self) -> None:
        if self._encoded_json is not None and (
            type(self._encoded_json) is not str or self.wire is None
        ):
            raise ValueError("encoded route response is invalid")
        if type(self.headers) is not tuple or len(self.headers) > 16:
            raise ValueError("route response headers are invalid")
        names: set[str] = set()
        for row in self.headers:
            if (
                type(row) is not tuple
                or len(row) != 2
                or type(row[0]) is not str
                or type(row[1]) is not str
                or not row[0]
                or not row[1]
                or len(row[0]) > 128
                or len(row[1]) > 1024
                or "\r" in row[0]
                or "\n" in row[0]
                or "\r" in row[1]
                or "\n" in row[1]
                or row[0].lower() in names
            ):
                raise ValueError("route response headers are invalid")
            names.add(row[0].lower())


#: A refusal body factory for the refusals this seam raises itself (415, 403, 413, 400, 500). It
#: receives the status and this seam's stable reason and returns the wire body the owning route
#: already answers with, or `None` for a bodiless response. Routes answer the same refusal with
#: four different shapes today -- bodiless, `{"error": code}`, `{"schema", "category"}` and a
#: typed envelope -- and unifying that is a wire change M23-34 owns. The seam therefore asks each
#: route for its own body rather than imposing one.
RefusalBody = Callable[[int, str], object | None]

#: Maps one typed refusal a route declared in `RoutePolicy.refusals` to the response it answers
#: with. A route that declares refusals must supply one; the seam does not guess a status from an
#: exception, because three routes derive it from the refusal's own `code` in three different ways.
RefusalMapper = Callable[[BaseException], RouteResult]

#: Computes one derived request attribute before any body byte is read -- a session handle, today --
#: or returns the refusal the route answers with. Returning a `RouteResult` means refused; any other
#: value is passed to the handler as its context. It runs after the origin check and before the body
#: so that an unauthorized caller cannot make this process materialize a request it may not send.
Prelude = Callable[[Any], object]

#: The handler this seam wraps. It receives the decoded request bytes (empty for a bodiless method)
#: and the prelude's context (`None` when the route declares no prelude), and returns a RouteResult.
#: Everything about aiohttp stays on this side of the boundary.
BodyHandler = Callable[[bytes, object], Awaitable[RouteResult]]


async def offload_route_handler(lane: str, operation: Callable[[], RouteResult]) -> RouteResult:
    def encoded() -> RouteResult:
        result = operation()
        # CRITICAL: JSON encoding can be as expensive as dispatch for a large projection. Encode
        # in the admitted worker, then keep HTTP response construction on the owning event loop.
        if result.wire is None:
            return result
        return replace(result, _encoded_json=json.dumps(result.wire))

    return await run_route_work(lane, encoded)


_BODY_METHODS = frozenset({"POST"})
_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Everything that differs between one owned JSON route and the next, as data."""

    path: str
    owner: str
    #: CRITICAL: the ownership *attribute name* stays per route rather than becoming one shared
    #: name. Each of the ten adapters chose its own before this seam existed, and a test or a host
    #: that looks for a specific one must keep finding it. Collapsing them to a single name is an
    #: observable change to the ownership protocol for no gain -- the value stamped on the handler
    #: is already the owning response schema, which is what distinguishes one owner from another.
    owner_attribute: str
    method: str = "POST"
    max_bytes: int | None = None
    origin: OriginRule = OriginRule.EXACT
    #: Typed refusals this route raises from its prelude, decoder or dispatch. Declaring one obliges
    #: the route to supply a `RefusalMapper`; see `register_owned_route`.
    refusals: tuple[type[BaseException], ...] = ()

    def __post_init__(self) -> None:
        if self.method not in _ALLOWED_METHODS:
            raise ValueError("an owned route is GET, POST or DELETE")
        if (self.method in _BODY_METHODS) != (self.max_bytes is not None):
            raise ValueError("exactly the body-carrying methods declare a byte budget")
        if self.max_bytes is not None and self.max_bytes < 1:
            raise ValueError("a byte budget is positive")


def _host_web() -> Any:
    return getattr(sys.modules.get("aiohttp"), "web", None)


def encoded_response(body: bytes, *, status: int, headers: dict[str, str]) -> Any:
    """Construct an already bounded, encoded response at the optional HTTP edge."""
    web = _host_web()
    if web is None:
        raise RuntimeError("HTTP response runtime is unavailable")
    return web.Response(body=body, status=status, headers=headers)


def streamed_response(*, status: int, headers: dict[str, str]) -> Any:
    """Construct a stream without taking its caller's cancellation or read ownership.

    CRITICAL: the media lease owner must retain prepare/write deadlines and discard its read on
    disconnect. Moving the stream loop here without that ownership leaks abandoned derivatives.
    """
    web = _host_web()
    if web is None:
        raise RuntimeError("HTTP response runtime is unavailable")
    return web.StreamResponse(status=status, headers=headers)


def host_web_and_routes() -> tuple[Any, Any] | None:
    """The lazy host lookup, once.

    CRITICAL: `sys.modules.get` rather than `import`. Importing `server` or `aiohttp` here would
    make the package unimportable outside ComfyUI, which breaks the pure-core boundary and every
    test that exercises an adapter without a host.
    """

    server_module = sys.modules.get("server")
    prompt_server = getattr(server_module, "PromptServer", None)
    web = _host_web()
    instance = getattr(prompt_server, "instance", None)
    routes = getattr(instance, "routes", None)
    if routes is None or web is None:
        return None
    return web, routes


def route_method_available(routes: Any, policy: RoutePolicy) -> bool:
    """Whether the host router can serve this policy's method at all.

    `register_owned_route` asks this itself, so a single-route adapter never needs it. An adapter
    that owns two methods on one path does: registering is a side effect on the host router, and
    discovering the second method is unavailable after the first is already installed leaves half
    a pair behind. Pre-scanning both is what keeps such a registrar all-or-nothing.
    """

    return callable(getattr(routes, policy.method.lower(), None))


def already_owned(routes: Any, policy: RoutePolicy, module: str) -> bool | None:
    """Whether this path and method are already taken, and if so whether by us.

    Returns `None` when the path is free. `True` means a handler this package registered is already
    there and registration is a no-op; `False` means something else owns the path and the route
    stays unregistered -- fail closed rather than replace a foreign handler.
    """

    for route in routes:
        if (
            getattr(route, "method", None) == policy.method
            and getattr(route, "path", None) == policy.path
        ):
            handler = getattr(route, "handler", None)
            return (
                callable(handler)
                and getattr(handler, policy.owner_attribute, None) == policy.owner
                and getattr(handler, "__module__", None) == module
            )
    return None


_PUBLIC_ORIGINS: PublicOrigins | None = None
_PUBLIC_ORIGINS_LOCK = threading.Lock()
_NOTED_REFUSALS: set[AdmissionReason] = set()
_NOTED_REFUSALS_LOCK = threading.Lock()


def public_origins() -> PublicOrigins:
    """The operator's configured targets, read once per process.

    CRITICAL: invalid configuration configures nothing and never widens anything; the direct
    listener rule keeps working. A partially valid list is not partially applied. The warning names
    only a category and a count, never a value, because the value is a deployment address.
    """

    global _PUBLIC_ORIGINS
    with _PUBLIC_ORIGINS_LOCK:
        if _PUBLIC_ORIGINS is None:
            configured = parse_public_origins(os.environ.get(PUBLIC_ORIGINS_VARIABLE))
            if configured.status not in (PublicOriginsStatus.UNSET, PublicOriginsStatus.VALID):
                _LOGGER.warning(
                    "H3 Context ignored %s: reason=%s entries=%d",
                    PUBLIC_ORIGINS_VARIABLE,
                    configured.status.value,
                    configured.entries,
                )
            _PUBLIC_ORIGINS = configured
        return _PUBLIC_ORIGINS


def _request_local_endpoint(request: Any) -> LocalEndpoint | None:
    """The socket this connection was accepted on: a fact of the server, not of the caller.

    CRITICAL: read from the transport, never from `Host`, `Forwarded` or `X-Forwarded-*`, and never
    from `PromptServer.address`, which records only the first of several `--listen` addresses.
    """

    get_extra_info = getattr(getattr(request, "transport", None), "get_extra_info", None)
    if not callable(get_extra_info):
        return None
    try:
        sockname = get_extra_info("sockname")
        sslcontext = get_extra_info("sslcontext")
    except Exception:
        return None
    return local_endpoint(sockname, sslcontext is not None)


def _note_refusal(reason: AdmissionReason) -> None:
    """Log each refusal category once per process, without any value from the request."""

    with _NOTED_REFUSALS_LOCK:
        if reason in _NOTED_REFUSALS:
            return
        _NOTED_REFUSALS.add(reason)
    if reason is AdmissionReason.HOST_UNTRUSTED:
        _LOGGER.warning(
            "H3 Context refused a browser request: reason=%s; for a proxy, port-mapped or "
            "custom-hostname deployment, list the page's exact origin in %s",
            reason.value,
            PUBLIC_ORIGINS_VARIABLE,
        )
    else:
        _LOGGER.warning("H3 Context refused a browser request: reason=%s", reason.value)


def request_admission_refusal(request: Any, rule: OriginRule) -> AdmissionReason | None:
    """`None` when the request may act for the origin it addressed, else the refusal category.

    CRITICAL: every header is read through `getall`, and exactly one value is required where one is
    required. A single `get("Origin")` would accept the owned origin *plus* another one, which no
    browser sends and any caller can construct; a single `get("Host")` would do the same for Host.
    """

    headers = getattr(request, "headers", None)
    getall = getattr(headers, "getall", None)
    if not callable(getall):
        return AdmissionReason.HOST_INVALID
    return admission_refusal(
        _REQUIREMENTS[rule],
        hosts=getall("Host", []),
        origins=getall("Origin", []),
        fetch_sites=getall("Sec-Fetch-Site", []),
        local=_request_local_endpoint(request),
        public=public_origins(),
    )


def origin_accepted(request: Any, rule: OriginRule) -> bool:
    """Whether the request may act for the origin it addressed, under `rule`."""

    reason = request_admission_refusal(request, rule)
    if reason is None:
        return True
    _note_refusal(reason)
    return False


async def read_bounded_body(request: Any, limit: int) -> bytes | None:
    """Read at most `limit` bytes, or `None` when the request exceeds it.

    CRITICAL: the budget is enforced while reading, not only against `Content-Length`. That header
    is a claim from the caller; a chunked request carries none at all. Deleting either check leaves
    the other one bypassable.
    """

    body = bytearray()
    while True:
        chunk = await request.content.read(limit + 1 - len(body))
        if type(chunk) is not bytes:
            raise ValueError("request body reader returned a non-bytes chunk")
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            return None


def content_length_verdict(request: Any, limit: int) -> str:
    """`"accepted"`, `"malformed"` or `"too_large"` for the declared `Content-Length`.

    A negative or non-integer length is a malformed request rather than an oversized one, and
    `comfyui_duration_resolution` was the one route of ten that said so. Keeping the distinction
    here preserves that answer and gives the other nine a stricter refusal than reaching the body
    reader with a nonsense header.
    """

    content_length = request.content_length
    if content_length is None:
        return "accepted"
    if isinstance(content_length, bool) or not isinstance(content_length, int):
        return "malformed"
    if content_length < 0:
        return "malformed"
    return "too_large" if content_length > limit else "accepted"


def register_owned_route(
    policy: RoutePolicy,
    module: str,
    handler: BodyHandler,
    refusal_body: RefusalBody,
    *,
    refusal_mapper: RefusalMapper | None = None,
    prelude: Prelude | None = None,
) -> bool:
    """Register one owned route through the shared edge. Idempotent.

    Returns `True` when the route is registered and owned by this package, `False` when the host is
    absent, the host router cannot serve this method, or a foreign handler already holds the path.
    """

    if policy.refusals and refusal_mapper is None:
        raise ValueError("a route that declares typed refusals must map them")
    found = host_web_and_routes()
    if found is None:
        return False
    web, routes = found
    decorator = getattr(routes, policy.method.lower(), None)
    if not callable(decorator):
        return False
    owned = already_owned(routes, policy, module)
    if owned is not None:
        return owned

    def refuse(status: int, reason: str) -> Any:
        body = refusal_body(status, reason)
        if body is None:
            return web.Response(status=status)
        return web.json_response(body, status=status)

    def serialize(result: RouteResult) -> Any:
        headers = dict(result.headers)
        if result.wire is None:
            if not headers:
                return web.Response(status=result.status)
            return web.Response(status=result.status, headers=headers)
        if result._encoded_json is not None:
            options = {"headers": headers} if headers else {}
            return web.json_response(
                result.wire, status=result.status, dumps=lambda _: result._encoded_json, **options
            )
        if not headers:
            return web.json_response(result.wire, status=result.status)
        return web.json_response(result.wire, status=result.status, headers=headers)

    @decorator(policy.path)
    async def owned_handler(request):  # type: ignore[no-untyped-def]
        if policy.method in _BODY_METHODS and request.content_type != "application/json":
            return refuse(415, "media_type_rejected")
        # CRITICAL: the origin check runs before the prelude and before the body is read, so a
        # foreign caller cannot make this process read a header it may not send or materialize a
        # request it was never entitled to send. Moving it later reopens M23-47's sidebar gap, where
        # the check sat after the decode and only covered some of the route's actions.
        if not origin_accepted(request, policy.origin):
            return refuse(403, "origin_rejected")
        try:
            context: object = None
            if prelude is not None:
                context = prelude(request)
                if type(context) is RouteResult:
                    return serialize(context)
            payload = b""
            limit = policy.max_bytes
            if limit is not None:
                verdict = content_length_verdict(request, limit)
                if verdict == "malformed":
                    return refuse(400, "invalid_request")
                if verdict == "too_large":
                    return refuse(413, "request_too_large")
                read = await read_bounded_body(request, limit)
                if read is None:
                    return refuse(413, "request_too_large")
                payload = read
            result = await handler(payload, context)
        except RouteWorkerCapacityError:
            return refuse(503, "route_worker_capacity")
        except policy.refusals as error:
            # `refusal_mapper` is not None here: the guard at the top of this function refuses a
            # policy that declares refusals without one.
            return serialize(refusal_mapper(error))  # type: ignore[misc]
        except (TypeError, ValueError, UnicodeDecodeError):
            # CRITICAL: a decode failure is the caller's fault only when the caller sent
            # something to decode. On a GET or a DELETE there is no body, so the same exception is
            # this process failing at something it does alone; answering 400 would blame the caller
            # and tell them to change a request that was correct.
            if policy.method not in _BODY_METHODS:
                return refuse(500, "internal_failure")
            return refuse(400, "invalid_request")
        except Exception:
            # CRITICAL: never let a filesystem, decoder or framework message reach a client. Those
            # strings carry host paths and internal state, and this is the only place every owned
            # route is guaranteed to pass through. Five of the ten routes this replaces had no such
            # guard, including the one that handles provider credentials.
            return refuse(500, "internal_failure")
        return serialize(result)

    # CRITICAL: the handler is defined in this module, so `__module__` would name the seam and the
    # already-owned check above -- which every adapter performs against its own module name -- would
    # stop recognising routes this package registered. Restoring the owning module keeps that
    # protocol byte-identical to the ten hand-written copies this replaces.
    owned_handler.__module__ = module
    setattr(owned_handler, policy.owner_attribute, policy.owner)
    return True


__all__ = [
    "BodyHandler",
    "OriginRule",
    "Prelude",
    "RefusalBody",
    "RefusalMapper",
    "RoutePolicy",
    "RouteResult",
    "already_owned",
    "content_length_verdict",
    "encoded_response",
    "host_web_and_routes",
    "origin_accepted",
    "offload_route_handler",
    "public_origins",
    "read_bounded_body",
    "request_admission_refusal",
    "route_method_available",
    "register_owned_route",
    "streamed_response",
]
