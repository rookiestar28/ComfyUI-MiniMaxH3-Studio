"""Owned application and route seam for explicit Production-output import."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import cast

from ..core.production_authoring_import import (
    MAX_PRODUCTION_AUTHORING_IMPORT_BYTES,
    PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    ProductionAuthoringImportRequest,
    ProductionAuthoringImportRequestV2,
    ProductionAuthoringImportResponse,
    ProductionAuthoringImportResponseV2,
    decode_production_authoring_import_json,
)
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .comfyui_authoring_workspace import (
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from .comfyui_media_runtime import current_authorized_media_runtime, media_runtime_lease
from .comfyui_production_workspace import ProductionWorkspaceRegistry
from .comfyui_route_seam import RoutePolicy, RouteResult, register_owned_route

PRODUCTION_AUTHORING_IMPORT_ROUTE = "/h3-context/v1/production/authoring-import"
PRODUCTION_AUTHORING_IMPORT_TIMEOUT_SECONDS = 30.0
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_production_authoring_import_v1__"


class ProductionAuthoringImportService:
    """Join the process's exact Production and Authoring authorities once."""

    def __init__(
        self,
        *,
        production_registry: ProductionWorkspaceRegistry,
        authoring_registry: AuthoringWorkspaceRegistry,
        media_runtime: Callable[[], QualifiedAVMediaAdapter | None] = (
            current_authorized_media_runtime
        ),
        lease: Callable[[], AbstractContextManager[None]] = media_runtime_lease,
    ) -> None:
        if (
            type(production_registry) is not ProductionWorkspaceRegistry
            or type(authoring_registry) is not AuthoringWorkspaceRegistry
            or not callable(media_runtime)
            or not callable(lease)
        ):
            raise TypeError("production authoring import dependencies are invalid")
        self._production = production_registry
        self._authoring = authoring_registry
        self._media_runtime = media_runtime
        self._lease = lease

    def dispatch(
        self,
        request: ProductionAuthoringImportRequest | ProductionAuthoringImportRequestV2,
        *,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ProductionAuthoringImportResponse | ProductionAuthoringImportResponseV2:
        from .media_runtime_manager import MediaRuntimeBusy

        try:
            with self._lease():
                adapter = self._media_runtime()
                if type(adapter) is not QualifiedAVMediaAdapter:
                    raise AuthoringWorkbenchError(503, "source_unavailable")
                return self._authoring.import_production_outputs(
                    request,
                    production_registry=self._production,
                    media_adapter=adapter,
                    deadline=deadline,
                    cancelled=cancelled,
                )
        except MediaRuntimeBusy:
            raise AuthoringWorkbenchError(503, "source_unavailable") from None


def build_production_authoring_import_service(
    *,
    production_registry: ProductionWorkspaceRegistry,
    authoring_registry: AuthoringWorkspaceRegistry,
) -> ProductionAuthoringImportService:
    return ProductionAuthoringImportService(
        production_registry=production_registry,
        authoring_registry=authoring_registry,
    )


def dispatch_production_authoring_import(
    request: ProductionAuthoringImportRequest | ProductionAuthoringImportRequestV2,
    *,
    deadline: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> ProductionAuthoringImportResponse | ProductionAuthoringImportResponseV2:
    from .composition_root import PRODUCTION_AUTHORING_IMPORT, get

    service = cast(
        ProductionAuthoringImportService,
        get(PRODUCTION_AUTHORING_IMPORT),
    )
    return service.dispatch(request, deadline=deadline, cancelled=cancelled)


@dataclass(frozen=True, slots=True)
class _RouteContext:
    deadline: float
    cancelled: Callable[[], bool]


def _prelude(request: object) -> _RouteContext:
    transport = getattr(request, "transport", None)

    def cancelled() -> bool:
        closing = getattr(transport, "is_closing", None)
        return bool(closing()) if callable(closing) else False

    return _RouteContext(
        deadline=time.monotonic() + PRODUCTION_AUTHORING_IMPORT_TIMEOUT_SECONDS,
        cancelled=cancelled,
    )


async def _act(payload: bytes, context: object) -> RouteResult:
    if type(context) is not _RouteContext:
        raise AuthoringWorkbenchError(500, "internal_invariant")
    request = decode_production_authoring_import_json(payload)
    # IMPORTANT: dispatch blocks (first-use activation, media probes, private copies); on the host
    # event loop it would stall every other request for up to the import deadline.
    response = await asyncio.to_thread(
        dispatch_production_authoring_import,
        request,
        deadline=context.deadline,
        cancelled=context.cancelled,
    )
    return RouteResult(200, response.to_wire())


def _refusal_body(_status: int, _reason: str) -> None:
    return None


def _refuse(error: BaseException) -> RouteResult:
    if isinstance(error, AuthoringWorkbenchError):
        return RouteResult(error.status)
    return RouteResult(400)


_ROUTE_POLICY = RoutePolicy(
    path=PRODUCTION_AUTHORING_IMPORT_ROUTE,
    owner=PRODUCTION_AUTHORING_IMPORT_REQUEST_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_PRODUCTION_AUTHORING_IMPORT_BYTES,
    refusals=(AuthoringWorkbenchError, ValueError),
)


def ensure_production_authoring_import_route_registered() -> bool:
    """Register the strict same-origin route without importing optional host modules."""

    return register_owned_route(
        _ROUTE_POLICY,
        __name__,
        _act,
        _refusal_body,
        refusal_mapper=_refuse,
        prelude=_prelude,
    )


__all__ = [
    "PRODUCTION_AUTHORING_IMPORT_ROUTE",
    "PRODUCTION_AUTHORING_IMPORT_TIMEOUT_SECONDS",
    "ProductionAuthoringImportService",
    "build_production_authoring_import_service",
    "dispatch_production_authoring_import",
    "ensure_production_authoring_import_route_registered",
]
