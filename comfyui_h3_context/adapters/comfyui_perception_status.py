"""Optional read-only finite perception host status boundary."""

from __future__ import annotations

from .comfyui_route_seam import OriginRule, RoutePolicy, RouteResult, register_owned_route
from .perception_settings import SETTINGS_REASONS, read_perception_host_status

PERCEPTION_HOST_STATUS_ROUTE = "/h3-context/v1/perception/status"
PERCEPTION_HOST_STATUS_SCHEMA = "h3.context.perception_host_status.v1"
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_perception_host_status_owner__"
_ROUTE_REGISTERED = False
_ROUTE_POLICY = RoutePolicy(
    path=PERCEPTION_HOST_STATUS_ROUTE,
    owner=PERCEPTION_HOST_STATUS_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    origin=OriginRule.EXACT_OR_ABSENT,
)


async def _status(_payload: bytes, _context: object) -> RouteResult:
    wire = read_perception_host_status()
    # CRITICAL: serialize only the closed finite projection. Returning a binding/settings object
    # or extra metadata here would expose operator paths through this otherwise read-only GET.
    if (
        type(wire) is not dict
        or any(type(key) is not str for key in wire)
        or set(wire) != {"schema", "state", "visual", "audio", "reason", "cancellation_observed"}
        or type(wire["schema"]) is not str
        or wire["schema"] != PERCEPTION_HOST_STATUS_SCHEMA
        or type(wire["state"]) is not str
        or wire["state"] not in {"configured", "not_configured", "refused"}
        or type(wire["reason"]) is not str
        or wire["reason"] not in SETTINGS_REASONS
        or any(
            type(wire[name]) is not bool for name in ("visual", "audio", "cancellation_observed")
        )
        or (
            wire["state"] == "configured"
            and (not (wire["visual"] or wire["audio"]) or wire["reason"] != "none")
        )
        or (
            wire["state"] != "configured"
            and (
                wire["visual"]
                or wire["audio"]
                or wire["cancellation_observed"]
                or wire["reason"] == "none"
            )
        )
    ):
        raise ValueError("perception_status_invalid")
    return RouteResult(200, wire)


def _refusal_body(status: int, reason: str) -> dict[str, str]:
    return {"error": "perception_status_unavailable" if status == 500 else reason}


def ensure_perception_host_status_route_registered() -> bool:
    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(_ROUTE_POLICY, __name__, _status, _refusal_body)
    return _ROUTE_REGISTERED
