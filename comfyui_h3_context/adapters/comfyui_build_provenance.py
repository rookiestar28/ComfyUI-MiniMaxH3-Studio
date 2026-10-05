"""Optional ComfyUI read-only build-provenance route."""

from __future__ import annotations

from ..core.build_provenance import (
    BUILD_PROVENANCE_RESPONSE_SCHEMA,
    SERVED_BUNDLE_PATH,
    BuildProvenanceError,
    load_build_provenance,
    observe_served_bundle,
)
from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    RouteResult,
    register_owned_route,
)

BUILD_PROVENANCE_ROUTE = "/h3-context/v1/build/provenance"
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_build_provenance_owner__"
_ROUTE_REGISTERED = False


_ROUTE_POLICY = RoutePolicy(
    path=BUILD_PROVENANCE_ROUTE,
    owner=BUILD_PROVENANCE_RESPONSE_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    # CRITICAL: a browser omits `Origin` on a same-origin GET, so this read-only route admits an
    # absent header. Before M23-47 it read no origin at all, which let any page on any origin read
    # this host's bundle digest and build inputs.
    origin=OriginRule.EXACT_OR_ABSENT,
    refusals=(BuildProvenanceError, OSError),
)


def _refusal_body(status: int, reason: str) -> dict[str, str]:
    return {"error": "provenance_unavailable" if status == 500 else reason}


def _refuse(_error: BaseException) -> RouteResult:
    # CRITICAL: the refusal names only that provenance is unavailable. A `BuildProvenanceError` or
    # an `OSError` carries the host path it failed on, which never reaches a client.
    return RouteResult(500, {"error": "provenance_unavailable"})


async def _provenance(_payload: bytes, _context: object) -> RouteResult:
    record = load_build_provenance()
    observed = observe_served_bundle(record, SERVED_BUNDLE_PATH)
    return RouteResult(
        200,
        {
            "schema": BUILD_PROVENANCE_RESPONSE_SCHEMA,
            "record": record,
            "served_bundle": observed,
        },
    )


def ensure_build_provenance_route_registered() -> bool:
    """Register one collision-safe GET route without importing optional host modules."""

    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _provenance,
        _refusal_body,
        refusal_mapper=_refuse,
    )
    return _ROUTE_REGISTERED


__all__ = ["BUILD_PROVENANCE_ROUTE", "ensure_build_provenance_route_registered"]
