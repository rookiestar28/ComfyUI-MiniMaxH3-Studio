"""Finite bounded portable project edge; no arbitrary filesystem, queue or provider port."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast

from ..core.project_document import (
    ProjectDocumentError,
    closed,
    identifier,
    project_bytes,
    project_json,
)
from .comfyui_route_seam import RoutePolicy, RouteResult, register_owned_route
from .composition_root import PROJECT_DOCUMENT, component
from .project_document_service import ProjectDocumentService
from .route_workers import run_route_work

PROJECT_DOCUMENT_ROUTE = "/h3-context/project-document"
MAX_PROJECT_REQUEST_BYTES = 5 * 1024 * 1024
MAX_PROJECT_RESPONSE_BYTES = 3 * 1024 * 1024
WORK_DEADLINE_SECONDS = 43.0
REQUEST_DEADLINE_SECONDS = 60.0
_SERVICE = component(PROJECT_DOCUMENT, ProjectDocumentService)
_INTENTS = {
    "export": {"intent", "owner", "planning", "title"},
    "open": {"intent", "document"},
    "discard": {"intent", "owner"},
    "relink": {"intent", "owner", "asset_id", "retained_id"},
}
_POLICY = RoutePolicy(
    path=PROJECT_DOCUMENT_ROUTE,
    owner="h3.context.project_document.response.v1",
    owner_attribute="__h3_context_project_document_v1__",
    max_bytes=MAX_PROJECT_REQUEST_BYTES,
    refusals=(ProjectDocumentError,),
)


def decode_project_action(data: bytes) -> dict[str, Any]:
    value = project_json(data, maximum_bytes=MAX_PROJECT_REQUEST_BYTES)
    if (
        type(value) is not dict
        or type(value.get("intent")) is not str
        or value["intent"] not in _INTENTS
    ):
        raise ProjectDocumentError("command_invalid")
    action = closed(value, _INTENTS[value["intent"]])
    if action["intent"] == "relink":
        identifier(action["asset_id"])
        from ..core.retained_assets import RetainedAssetError, require_asset_id

        try:
            require_asset_id(action["retained_id"])
        except RetainedAssetError:
            raise ProjectDocumentError("command_invalid") from None
    return action


def _refuse(error: BaseException) -> RouteResult:
    code = error.code if isinstance(error, ProjectDocumentError) else "internal_failure"
    status = {
        "project_conflict": 409,
        "media_mismatch": 409,
        "project_capacity": 429,
        "project_unavailable": 404,
        "editor_not_initialized": 422,
        "relink_unavailable": 422,
        "cancelled": 499,
        "timed_out": 504,
        "internal_failure": 500,
        "document_size": 413,
    }.get(code, 400)
    return RouteResult(
        status,
        {"schema": "h3.context.project_document.error.v1", "code": code},
        (("Cache-Control", "no-store"),),
    )


class _Cancellation:
    def __init__(self, request: object) -> None:
        self.transport = getattr(request, "transport", None)
        self.event = threading.Event()

    def cancelled(self) -> bool:
        closing = getattr(self.transport, "is_closing", None)
        return self.event.is_set() or (bool(closing()) if callable(closing) else True)


async def _act(data: bytes, context: object) -> RouteResult:
    cancellation = cast(_Cancellation, context)
    action = decode_project_action(data)
    service = _SERVICE()
    deadline = time.monotonic() + WORK_DEADLINE_SECONDS

    def work() -> RouteResult:
        if cancellation.cancelled():
            raise ProjectDocumentError("cancelled")
        intent = action["intent"]
        response: dict[str, Any]
        if intent == "discard":
            service.discard_open_response({"owner": action["owner"]})
            response = {"schema": "h3.context.project_document.discard.v1"}
        elif intent == "open":
            response = service.open_document(action["document"], cancelled=cancellation.cancelled)
        elif intent == "export":
            response = service.snapshot_document(
                action["owner"], action["planning"], action["title"]
            )
        else:
            response = service.relink_document(
                action["owner"],
                action["asset_id"],
                action["retained_id"],
                deadline=deadline,
                cancelled=cancellation.cancelled,
            )
        encoded = project_bytes(response, maximum_bytes=MAX_PROJECT_RESPONSE_BYTES).decode("utf-8")
        if cancellation.cancelled():
            if intent == "open":
                service.discard_open_response(response)
            raise ProjectDocumentError("cancelled")
        return RouteResult(200, response, (("Cache-Control", "no-store"),), encoded)

    task = asyncio.create_task(run_route_work("project_document", work))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=REQUEST_DEADLINE_SECONDS)
    except (asyncio.CancelledError, TimeoutError):
        cancellation.event.set()

        def discard_late(done: asyncio.Task[RouteResult]) -> None:
            if done.cancelled():
                return
            try:
                result = done.result()
            except BaseException:
                return
            if action["intent"] == "open" and type(result.wire) is dict:
                service.discard_open_response(result.wire)

        task.add_done_callback(discard_late)
        current_task = asyncio.current_task()
        if current_task is not None and getattr(current_task, "cancelling", lambda: 0)():
            raise
        raise ProjectDocumentError("timed_out") from None


def ensure_project_document_route_registered() -> bool:
    return register_owned_route(
        _POLICY,
        __name__,
        _act,
        lambda status, reason: RouteResult(
            status,
            {"schema": "h3.context.project_document.error.v1", "code": reason},
            (("Cache-Control", "no-store"),),
        ),
        refusal_mapper=_refuse,
        prelude=_Cancellation,
    )
