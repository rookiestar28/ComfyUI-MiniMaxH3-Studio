"""Optional ComfyUI adapter for deterministic Sidebar duration resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..core.length import MILLISECONDS_PER_SECOND, resolve_milliseconds
from ..core.official_context_ir import (
    MAX_OFFICIAL_DURATION_SECONDS,
    MIN_OFFICIAL_DURATION_SECONDS,
)
from .comfyui_route_seam import RoutePolicy, RouteResult, register_owned_route

DURATION_RESOLUTION_REQUEST_SCHEMA = "h3.context.duration_resolution_request.v1"
DURATION_RESOLUTION_RESPONSE_SCHEMA = "h3.context.duration_resolution.v1"
DURATION_RESOLUTION_ROUTE = "/h3-context/v1/duration/resolve"
MAX_DURATION_RESOLUTION_BYTES = 256
_DURATION_REQUEST_KEYS = frozenset({"schema", "requested_seconds"})
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_route_owner__"
_ROUTE_REGISTERED = False


class DurationResolutionError(ValueError):
    """A stable content-free refusal for a malformed duration request."""

    def __init__(self, code: str = "invalid_request") -> None:
        super().__init__(code)
        self.code = code


def decode_duration_resolution_json(raw: bytes) -> Mapping[str, object]:
    """Decode one bounded JSON request; body-size enforcement belongs to the route."""

    if not isinstance(raw, bytes):
        raise DurationResolutionError()

    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DurationResolutionError()
            result[key] = value
        return result

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_object)
    except (DurationResolutionError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DurationResolutionError() from error
    if not isinstance(decoded, dict):
        raise DurationResolutionError()
    return decoded


_ROUTE_POLICY = RoutePolicy(
    path=DURATION_RESOLUTION_ROUTE,
    owner=DURATION_RESOLUTION_RESPONSE_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_DURATION_RESOLUTION_BYTES,
    refusals=(DurationResolutionError,),
)


def _refusal_body(status: int, reason: str) -> dict[str, str]:
    # The route has always answered an unsupported media type with the generic code rather than a
    # media-specific one. Keeping that mapping here is what lets the shared seam serve this route
    # without moving a byte of its wire.
    return {"error": "invalid_request" if status == 415 else reason}


def _refuse(error: BaseException) -> RouteResult:
    code = getattr(error, "code", None)
    return RouteResult(400, {"error": code if isinstance(code, str) else "invalid_request"})


async def _resolve(payload: bytes, _context: object) -> RouteResult:
    return RouteResult(200, resolve_duration_request(decode_duration_resolution_json(payload)))


def ensure_duration_resolution_route_registered() -> bool:
    """Register the optional route from host-owned modules when available."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _resolve,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


def resolve_duration_request(payload: Mapping[str, object]) -> dict[str, object]:
    """Resolve one closed Sidebar duration request into its backend-owned length."""

    if set(payload) != _DURATION_REQUEST_KEYS:
        raise DurationResolutionError()
    if payload["schema"] != DURATION_RESOLUTION_REQUEST_SCHEMA:
        raise DurationResolutionError()
    requested_seconds = payload["requested_seconds"]
    if (
        isinstance(requested_seconds, bool)
        or not isinstance(requested_seconds, int)
        or not MIN_OFFICIAL_DURATION_SECONDS <= requested_seconds <= MAX_OFFICIAL_DURATION_SECONDS
    ):
        raise DurationResolutionError()
    resolved = resolve_milliseconds(requested_seconds * MILLISECONDS_PER_SECOND)
    return {
        "schema": DURATION_RESOLUTION_RESPONSE_SCHEMA,
        "requested_seconds": requested_seconds,
        "requested_milliseconds": resolved.requested_milliseconds,
        "effective_milliseconds": resolved.delivered_milliseconds,
        "frame_count": resolved.frame_count,
        "snapped": resolved.snapped,
    }


__all__ = [
    "DURATION_RESOLUTION_REQUEST_SCHEMA",
    "DURATION_RESOLUTION_RESPONSE_SCHEMA",
    "DURATION_RESOLUTION_ROUTE",
    "MAX_DURATION_RESOLUTION_BYTES",
    "DurationResolutionError",
    "decode_duration_resolution_json",
    "ensure_duration_resolution_route_registered",
    "resolve_duration_request",
]
