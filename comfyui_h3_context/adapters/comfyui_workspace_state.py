"""Optional bounded recovery route; caller headers never select a durable owner."""

from __future__ import annotations

from ..core.durable_workspace_state import DurableStateError
from .comfyui_route_seam import (
    RoutePolicy,
    RouteResult,
    offload_route_handler,
    register_owned_route,
)
from .composition_root import WORKSPACE_STATE, component
from .recovery_owner import RecoveryOwner, request_is_loopback
from .workspace_state_service import (
    MAX_ACTION_BYTES,
    RESPONSE_SCHEMA,
    WorkspaceStateService,
    decode_state_action,
    unavailable_projection,
)

WORKSPACE_STATE_ROUTE = "/h3-context/workspace-state"
workspace_state_service = component(WORKSPACE_STATE, WorkspaceStateService)
_ROUTE_REGISTERED = False
_POLICY = RoutePolicy(
    path=WORKSPACE_STATE_ROUTE,
    owner=RESPONSE_SCHEMA,
    owner_attribute="_h3_workspace_state_owner",
    max_bytes=MAX_ACTION_BYTES,
    refusals=(DurableStateError,),
)
_EDGE_CODES = frozenset(
    {
        "media_type_rejected",
        "origin_rejected",
        "request_too_large",
        "invalid_request",
        "internal_failure",
        "route_worker_capacity",
    }
)
_FORBIDDEN = frozenset(
    {
        "host_unqualified",
        "filesystem_unqualified",
        "owner_changed",
        "owner_mismatch",
        "storage_unsafe",
    }
)
_CONFLICT = frozenset(
    {
        "revision_conflict",
        "revision_exhausted",
        "recovery_disabled",
        "record_unavailable",
        "owner_busy",
        "catalog_busy",
        "quota_directory",
        "quota_owners",
        "quota_records",
        "quota_bytes",
        "storage_unverified",
        "version_unsupported",
        "snapshot_corrupt",
        "snapshot_unavailable",
    }
)


def _refusal_body(_status: int, reason: str) -> dict[str, object]:
    code = reason if reason in DurableStateError.CODES | _EDGE_CODES else "internal_failure"
    return {
        "schema": RESPONSE_SCHEMA,
        "projection": unavailable_projection(),
        "error": code,
        "recovered": None,
    }


def _refusal(error: BaseException) -> RouteResult:
    if type(error) is not DurableStateError:
        return RouteResult(500, _refusal_body(500, "internal_failure"))
    code = str(error)
    status = (
        403
        if code in _FORBIDDEN
        else 409
        if code in _CONFLICT
        else 503
        if code in {"storage_unavailable", "service_closed"}
        else 400
    )
    return RouteResult(status, _refusal_body(status, code), (("Cache-Control", "no-store"),))


def _owner_prelude(request: object) -> RecoveryOwner:
    # CRITICAL: origin admission precedes this guard; real socket/peer and server facts precede
    # body/storage access. Host, forwarded and profile headers can never grant recovery access.
    if not request_is_loopback(request):
        raise DurableStateError("host_unqualified")
    return workspace_state_service().qualify()


async def _apply_action(payload: bytes, context: object) -> RouteResult:
    def dispatch() -> RouteResult:
        if type(context) is not RecoveryOwner:
            raise DurableStateError("host_unqualified")
        service = workspace_state_service()
        wire = service.dispatch(decode_state_action(payload), admitted_owner=context)
        return RouteResult(200, wire, (("Cache-Control", "no-store"),))

    return await offload_route_handler("coordinator", dispatch)


def ensure_workspace_state_route_registered() -> bool:
    global _ROUTE_REGISTERED
    _ROUTE_REGISTERED = register_owned_route(
        _POLICY,
        __name__,
        _apply_action,
        _refusal_body,
        refusal_mapper=_refusal,
        prelude=_owner_prelude,
    )
    return _ROUTE_REGISTERED
