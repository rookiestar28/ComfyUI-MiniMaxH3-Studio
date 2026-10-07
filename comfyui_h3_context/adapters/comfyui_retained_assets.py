"""Owned retained commands and separate explicit bounded MP4 preview edge."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, cast

from ..core.durable_workspace_state import json_bytes
from ..core.retained_assets import RetainedAssetError
from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    already_owned,
    content_length_verdict,
    encoded_response,
    host_web_and_routes,
    origin_accepted,
    read_bounded_body,
    route_method_available,
)
from .composition_root import RETAINED_ASSETS, component
from .media_preview_authority import MAX_MEDIA_PREVIEW_RESPONSE_BYTES
from .recovery_owner import RecoveryOwner, request_is_loopback
from .retained_asset_service import (
    MAX_ACTION_BYTES,
    RESPONSE_SCHEMA,
    STATUS_SCHEMA,
    RetainedAssetService,
    decode_retained_action,
    decode_retained_preview,
)
from .retained_asset_source import check_retention_budget
from .route_workers import RouteWorkerCapacityError, run_route_work

RETAINED_ASSETS_ROUTE = "/h3-context/retained-assets"
RETAINED_PREVIEW_ROUTE = RETAINED_ASSETS_ROUTE + "/preview"
WORK_DEADLINE_SECONDS = 43.0
REQUEST_DEADLINE_SECONDS = 60.0
_OWNER = "__h3_context_retained_assets_v1__"
_SERVICE = component(RETAINED_ASSETS, RetainedAssetService)
_CLAIM = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_ACTION_POLICY = RoutePolicy(
    path=RETAINED_ASSETS_ROUTE,
    owner=RESPONSE_SCHEMA,
    owner_attribute=_OWNER,
    max_bytes=MAX_ACTION_BYTES,
)
_PREVIEW_POLICY = RoutePolicy(
    path=RETAINED_PREVIEW_ROUTE,
    owner=RESPONSE_SCHEMA,
    owner_attribute=_OWNER,
    max_bytes=MAX_ACTION_BYTES,
)
_POLICIES = (_ACTION_POLICY, _PREVIEW_POLICY)


class _RouteTable(Protocol):
    def post(
        self, path: str
    ) -> Callable[[Callable[[Any], Awaitable[Any]]], Callable[[Any], Awaitable[Any]]]: ...


class _Cancellation:
    def __init__(self, request: object) -> None:
        self._transport = getattr(request, "transport", None)
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        closing = getattr(self._transport, "is_closing", None)
        try:
            return self._event.is_set() or (bool(closing()) if callable(closing) else True)
        except Exception:
            return True


@dataclass(slots=True)
class _WorkResult:
    body: bytes | bytearray
    wire: dict[str, Any] | None = None
    audio: str | None = None

    def discard(self, service: RetainedAssetService) -> None:
        if isinstance(self.body, bytearray):
            self.body.clear()
        if self.wire is not None:
            service.discard_response(self.wire)


def retained_assets_busy() -> bool:
    return _CLAIM.locked()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h3-retained-assets")
        return _EXECUTOR


def _status(code: str) -> int:
    if code in {
        "origin_rejected",
        "host_unqualified",
        "filesystem_unqualified",
        "storage_unsafe",
        "owner_changed",
        "owner_mismatch",
    }:
        return 403
    if code in {"asset_unavailable", "lease_invalid"}:
        return 404
    if code in {
        "revision_conflict",
        "recovery_disabled",
        "source_stale",
        "asset_changed",
        "asset_protected",
        "owner_busy",
        "catalog_busy",
    } or code.startswith("quota_"):
        return 409
    if code == "media_unqualified":
        return 422
    if code == "work_busy":
        return 429
    if code == "cancelled":
        return 499
    if code == "timed_out":
        return 504
    if code in {"storage_unavailable", "storage_unverified", "catalog_corrupt", "service_closed"}:
        return 503
    if code == "internal_failure":
        return 500
    return 400


def _failure(code: str, status: int | None = None) -> Any:
    body = json_bytes(
        {
            "schema": RESPONSE_SCHEMA,
            "error": code,
            "retained_id": None,
            "restored": None,
            "cleanup": None,
            "projection": {
                "schema": STATUS_SCHEMA,
                "supported": False,
                "enabled": False,
                "revision": 0,
                "count": 0,
                "charged_bytes": 0,
                "remnant_count": 0,
                "protected": 0,
                "assets": [],
            },
        }
    )
    return encoded_response(
        body,
        status=_status(code) if status is None else status,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _work(
    service: RetainedAssetService,
    owner: RecoveryOwner,
    command: dict[str, object] | str,
    deadline: float,
    cancellation: _Cancellation,
) -> _WorkResult:
    result = None
    try:
        if isinstance(command, str):
            body, audio = service.preview(
                command,
                deadline=deadline,
                cancelled=cancellation.is_cancelled,
                admitted_owner=owner,
            )
            result = _WorkResult(body, audio=audio)
            if not 1 <= len(body) <= MAX_MEDIA_PREVIEW_RESPONSE_BYTES:
                raise RetainedAssetError("media_unqualified")
        else:
            wire = service.dispatch(
                command,
                deadline=deadline,
                cancelled=cancellation.is_cancelled,
                admitted_owner=owner,
            )
            # IMPORTANT: own the fresh response before encoding. A serializer failure after
            # restore must still revoke/release its new use rather than strand an unseen lease.
            result = _WorkResult(b"", wire=wire)
            result.body = json_bytes(wire)
        check_retention_budget(deadline, cancellation.is_cancelled)
        if service.qualify() != owner:
            raise RetainedAssetError("owner_changed")
        return result
    except BaseException:
        if result is not None:
            result.discard(service)
        raise


def _abandoned(future: Future[_WorkResult], service: RetainedAssetService) -> None:
    try:
        future.result().discard(service)
    except Exception:
        # Consume an abandoned finite worker failure; the service keeps failed cleanup protected.
        return
    finally:
        _CLAIM.release()


async def _serve(request: Any, *, preview: bool) -> Any:
    started = time.monotonic()
    deadline, response_deadline = (
        started + WORK_DEADLINE_SECONDS,
        started + REQUEST_DEADLINE_SECONDS,
    )
    cancellation = _Cancellation(request)
    if request.content_type != "application/json":
        return _failure("media_type_rejected", 415)
    # SECURITY: caller headers never select an owner. Origin, actual socket and server policy
    # precede body IO/private storage; moving any check later lets foreign requests touch media.
    if not origin_accepted(request, OriginRule.EXACT) or not request_is_loopback(request):
        return _failure("origin_rejected", 403)
    getall = getattr(getattr(request, "headers", None), "getall", None)
    if getattr(request, "query_string", "") or (callable(getall) and getall("Range", [])):
        return _failure("invalid_request")
    future: Future[_WorkResult] | None = None
    result: _WorkResult | None = None
    service: RetainedAssetService | None = None
    claimed, delivered = False, False
    try:
        service = _SERVICE()
        owner = service.qualify()
        check_retention_budget(deadline, cancellation.is_cancelled)
        verdict = content_length_verdict(request, MAX_ACTION_BYTES)
        if verdict != "accepted":
            return _failure(
                "request_too_large" if verdict == "too_large" else "invalid_request",
                413 if verdict == "too_large" else 400,
            )
        payload = await asyncio.wait_for(
            read_bounded_body(request, MAX_ACTION_BYTES),
            timeout=max(0, deadline - time.monotonic()),
        )
        if payload is None:
            return _failure("request_too_large", 413)
        command = decode_retained_preview(payload) if preview else decode_retained_action(payload)
        check_retention_budget(deadline, cancellation.is_cancelled)
        if type(command) is dict and command["intent"] == "release":
            # CRITICAL: hiding a view aborts media work and revokes its handle. Admission to that
            # cleanup cannot sit behind the media claim, or hidden fresh copies remain leased.
            def revoke() -> bytes:
                wire = service.dispatch(command, deadline=deadline, admitted_owner=owner)
                return json_bytes(wire)

            body = await asyncio.wait_for(
                run_route_work("retained_cleanup", revoke),
                timeout=max(0, response_deadline - time.monotonic()),
            )
            return encoded_response(
                body,
                status=200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if not _CLAIM.acquire(blocking=False):
            return _failure("work_busy")
        claimed = True
        future = _executor().submit(_work, service, owner, command, deadline, cancellation)
        while not future.done():
            check_retention_budget(deadline, cancellation.is_cancelled)
            await asyncio.sleep(0.02)
        result = future.result()
        check_retention_budget(deadline, cancellation.is_cancelled)
        if time.monotonic() >= response_deadline:
            raise RetainedAssetError("timed_out")
        if _SERVICE() is not service or service.qualify() != owner:
            raise RetainedAssetError("owner_changed")
        headers = {
            "Content-Type": "video/mp4" if preview else "application/json",
            "Content-Length": str(len(result.body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if preview:
            headers.update(
                {
                    "Content-Disposition": 'inline; filename="h3-retained-preview.mp4"',
                    "X-H3-Context-Embedded-Audio": result.audio or "unavailable",
                }
            )
        response = encoded_response(bytes(result.body), status=200, headers=headers)
        delivered = True
        return response
    except RetainedAssetError as error:
        return _failure(error.code)
    except RouteWorkerCapacityError:
        return _failure("work_busy")
    except (asyncio.TimeoutError, TimeoutError):
        return _failure("timed_out")
    except asyncio.CancelledError:
        cancellation.cancel()
        raise
    except Exception:
        return _failure("internal_failure")
    finally:
        if claimed:
            if future is not None and not future.done() and service is not None:
                cancellation.cancel()
                # CRITICAL: cancellation cannot kill a thread. Hold this non-queued claim until
                # the real worker exits and discards its fresh use/bytes, not until HTTP returns.
                abandoned_service = service
                future.add_done_callback(lambda done: _abandoned(done, abandoned_service))
            else:
                try:
                    if not delivered and future is not None and service is not None:
                        _abandoned(future, service)
                        claimed = False
                finally:
                    if claimed:
                        _CLAIM.release()


def ensure_retained_assets_routes_registered() -> bool:
    found = host_web_and_routes()
    if found is None:
        return False
    routes = cast(_RouteTable, found[1])
    ownership = [already_owned(routes, policy, __name__) for policy in _POLICIES]
    if any(value is False for value in ownership) or not all(
        route_method_available(routes, policy) for policy in _POLICIES
    ):
        return False
    # IMPORTANT: preflight both owners before declaring either route. Keep explicit declarations
    # so the source route census cannot silently lose these independently owned media edges.
    if not ownership[0]:

        @routes.post(RETAINED_ASSETS_ROUTE)
        async def retained_assets_action(request: Any) -> Any:
            return await _serve(request, preview=False)

        setattr(retained_assets_action, _OWNER, RESPONSE_SCHEMA)
    if not ownership[1]:

        @routes.post(RETAINED_PREVIEW_ROUTE)
        async def retained_assets_preview(request: Any) -> Any:
            return await _serve(request, preview=True)

        setattr(retained_assets_preview, _OWNER, RESPONSE_SCHEMA)
    return True
