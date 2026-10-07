"""Qualified recovery edge; bounded commands never select a server path or owner."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast

from ..core.editor_recovery import (
    MAX_RECOVERY_REVISION,
    EditorRecoveryError,
    recovery_json,
    require_project,
)
from ..core.project_document import ProjectDocumentError, closed, project_bytes
from ..core.retained_assets import RetainedAssetError
from .comfyui_route_seam import RoutePolicy, RouteResult, register_owned_route
from .composition_root import EDITOR_RECOVERY, component
from .editor_recovery_service import EditorRecoveryService
from .recovery_owner import RecoveryOwner, request_is_loopback
from .route_workers import run_route_work

MAX_RECOVERY_REQUEST_BYTES = 5 * 1024 * 1024
MAX_RECOVERY_RESPONSE_BYTES = 3 * 1024 * 1024
WORK_DEADLINE_SECONDS = 43.0
REQUEST_DEADLINE_SECONDS = 60.0
_SERVICE = component(EDITOR_RECOVERY, EditorRecoveryService)
_INTENTS = {
    "status": {"intent", "project_id"},
    "list": {"intent"},
    "settings": {"intent", "enabled", "include_video", "expected_revision"},
    "watch": {"intent", "owner", "planning", "title", "project_id"},
    "flush": {"intent", "project_id"},
    "detach": {"intent", "project_id", "discard"},
    "restore": {"intent", "project_id"},
    "clear": {"intent", "project_id"},
}
_POLICY = RoutePolicy(
    path="/h3-context/project-recovery",
    owner="h3.context.project_recovery.response.v1",
    owner_attribute="__h3_context_project_recovery_v1__",
    max_bytes=MAX_RECOVERY_REQUEST_BYTES,
    refusals=(EditorRecoveryError, ProjectDocumentError, RetainedAssetError),
)


def decode_recovery_action(data: bytes) -> dict[str, Any]:
    value = recovery_json(data, maximum_bytes=MAX_RECOVERY_REQUEST_BYTES)
    if (
        type(value) is not dict
        or type(value.get("intent")) is not str
        or value["intent"] not in _INTENTS
    ):
        raise EditorRecoveryError("command_invalid")
    try:
        action = closed(value, _INTENTS[value["intent"]])
    except ProjectDocumentError:
        raise EditorRecoveryError("command_invalid") from None
    for field in ("enabled", "include_video", "discard"):
        if field in action and type(action[field]) is not bool:
            raise EditorRecoveryError("command_invalid")
    if "expected_revision" in action and (
        type(action["expected_revision"]) is not int
        or not 0 <= action["expected_revision"] <= MAX_RECOVERY_REVISION
    ):
        raise EditorRecoveryError("command_invalid")
    if "project_id" in action and (
        action["project_id"] is not None or action["intent"] not in {"status", "watch"}
    ):
        require_project(action["project_id"])
    return action


def _error(status: int, code: str) -> RouteResult:
    return RouteResult(
        status,
        {"schema": "h3.context.project_recovery.error.v1", "code": code},
        (("Cache-Control", "no-store"),),
    )


def _refuse(error: BaseException) -> RouteResult:
    code = (
        error.code
        if isinstance(error, (EditorRecoveryError, ProjectDocumentError, RetainedAssetError))
        else "internal_failure"
    )
    status = (
        403
        if code
        in {
            "host_unqualified",
            "filesystem_unqualified",
            "owner_changed",
            "owner_mismatch",
            "storage_unsafe",
        }
        else 409
        if code
        in {
            "dirty_protected",
            "active_project",
            "revision_conflict",
            "project_conflict",
            "recovery_disabled",
            "catalog_busy",
            "owner_busy",
        }
        else 429
        if code.startswith("quota_") or code == "project_capacity"
        else 404
        if code == "project_unavailable"
        else 503
        if code in {"storage_unavailable", "service_closed"}
        else 499
        if code == "cancelled"
        else 504
        if code == "timed_out"
        else 500
        if code == "internal_failure"
        else 400
    )
    return _error(status, code)


class _Cancellation:
    def __init__(self, request: object, owner: RecoveryOwner) -> None:
        self.transport, self.owner, self.event = (
            getattr(request, "transport", None),
            owner,
            threading.Event(),
        )

    def cancelled(self) -> bool:
        closing = getattr(self.transport, "is_closing", None)
        return self.event.is_set() or (bool(closing()) if callable(closing) else True)


def _prelude(request: object) -> _Cancellation:
    # SECURITY: socket and server qualification precede body access; headers cannot select an owner.
    if not request_is_loopback(request):
        raise EditorRecoveryError("host_unqualified")
    return _Cancellation(request, _SERVICE().qualify())


async def _act(data: bytes, context: object) -> RouteResult:
    cancellation = cast(_Cancellation, context)
    action, service = decode_recovery_action(data), _SERVICE()
    deadline = time.monotonic() + WORK_DEADLINE_SECONDS

    def work() -> RouteResult:
        if cancellation.cancelled():
            raise EditorRecoveryError("cancelled")
        if service.qualify() != cancellation.owner:
            raise EditorRecoveryError("owner_changed")
        intent = action["intent"]
        if intent in {"status", "list"}:
            response = service.status(action.get("project_id"))
        elif intent == "settings":
            response = service.settings(
                action["enabled"],
                action["include_video"],
                expected_revision=action["expected_revision"],
            )
        elif intent == "watch":
            response = service.watch(
                action["owner"],
                action["planning"],
                action["title"],
                project_id=action["project_id"],
            )
        elif intent == "flush":
            response = service.flush(
                action["project_id"], deadline=deadline, cancelled=cancellation.cancelled
            )
        elif intent == "detach":
            response = service.detach(action["project_id"], discard=action["discard"])
        elif intent == "restore":
            response = service.restore(
                action["project_id"], deadline=deadline, cancelled=cancellation.cancelled
            )
        else:
            response = service.clear(action["project_id"])
        try:
            encoded = project_bytes(response, maximum_bytes=MAX_RECOVERY_RESPONSE_BYTES).decode(
                "utf-8"
            )
            if cancellation.cancelled():
                raise EditorRecoveryError("cancelled")
        except BaseException:
            # IMPORTANT: a staged restore is undelivered when encoding or transport fails.
            if intent == "restore":
                service.projects.discard_open_response(response)
            raise
        return RouteResult(200, response, (("Cache-Control", "no-store"),), encoded)

    task = asyncio.create_task(run_route_work("editor_recovery", work))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=REQUEST_DEADLINE_SECONDS)
    except (asyncio.CancelledError, TimeoutError):
        cancellation.event.set()

        def discard_late(done: asyncio.Task[RouteResult]) -> None:
            if not done.cancelled() and action["intent"] == "restore":
                try:
                    response = done.result().wire
                    if type(response) is dict:
                        service.projects.discard_open_response(response)
                except BaseException:
                    return

        task.add_done_callback(discard_late)
        current_task = asyncio.current_task()
        if current_task is not None and getattr(current_task, "cancelling", lambda: 0)():
            raise
        raise EditorRecoveryError("timed_out") from None


def ensure_editor_recovery_route_registered() -> bool:
    return register_owned_route(
        _POLICY, __name__, _act, _error, refusal_mapper=_refuse, prelude=_prelude
    )
