"""Same-origin, capability-fenced routes for private Authoring derivative leases."""

from __future__ import annotations

import asyncio
import atexit
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from ..core.authoring_media import (
    ERROR_SCHEMA,
    MAX_CONTROL_BYTES,
    REQUEST_SCHEMA,
    RETAINED_DERIVATIVE_IDLE_SECONDS,
    CreateLeaseRequest,
    LeaseCommand,
    MediaLeaseError,
    decode_lease_request,
)
from .authoring_media_leases import LeaseRead, MediaLeaseAuthority
from .comfyui_route_seam import (
    OriginRule,
    encoded_response,
    host_web_and_routes,
    origin_accepted,
    streamed_response,
)


class _RouteTable(Protocol):
    def __iter__(self) -> Iterator[Any]: ...

    def post(
        self,
        path: str,
    ) -> Callable[[Callable[[Any], Awaitable[Any]]], Callable[[Any], Awaitable[Any]]]: ...


LEASE_ROUTE = "/h3-context/v1/authoring/media-source-leases"
CAPABILITY_HEADER = "X-H3-Context-Media-Lease"
GEOMETRY_HEADER = "X-H3-Context-Media-Geometry"
_MAX_GEOMETRY_HEADER_BYTES = 256
_PLAYBACK_HANDOFF_SECONDS = 5.0
_OWNER = "__h3_context_authoring_media_leases_v1__"
_CAPABILITY = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {
    "invalid_request": 400,
    "authority_mismatch": 404,
    "stale": 409,
    "lease_gone": 410,
    "unsupported": 422,
    "resource_limit": 413,
    "busy": 429,
    "cancelled": 499,
    "timeout": 504,
    "generation_failed": 422,
    "internal_failure": 500,
}


class _Cancellation:
    def __init__(self) -> None:
        self.event = threading.Event()

    def is_cancelled(self) -> bool:
        return self.event.is_set()


@dataclass(repr=False)
class _Result:
    receipt: dict[str, object]
    capability: str = field(repr=False)
    read: LeaseRead | None = field(default=None, repr=False)


def _application_claim(request: CreateLeaseRequest) -> Any:
    from .authoring_derivative_generator import (
        AuthoringDerivativeGenerator,
        AuthoringDerivativeGeneratorError,
    )
    from .comfyui_authoring_media_preview import current_authoring_media_preview_adapter
    from .comfyui_authoring_workspace import (
        AuthoringWorkbenchError,
        admit_authoring_media_derivative,
    )
    from .comfyui_media_runtime import authorized_authoring_derivative_runtime

    try:
        generator = authorized_authoring_derivative_runtime()
        if generator is None and request.derivative_kind in {"image_proxy", "thumbnail"}:
            generator = AuthoringDerivativeGenerator.for_images()
        return admit_authoring_media_derivative(
            request,
            generator=generator,
            media_adapter=current_authoring_media_preview_adapter(),
        )
    except AuthoringDerivativeGeneratorError as exc:
        raise MediaLeaseError(exc.code) from None
    except AuthoringWorkbenchError as exc:
        reason = (
            "stale" if exc.code in {"workspace_gone", "preview_stale"} else "authority_mismatch"
        )
        raise MediaLeaseError(reason) from None


class MediaLeaseRouteService:
    """One no-queue I/O worker; release remains available during a long generation."""

    def __init__(self, authority: MediaLeaseAuthority | None = None) -> None:
        # The product keeps a derivative body past its last lease: every accepted edit ends the
        # leases of the superseded snapshot, and the next snapshot asks for the same bytes.
        self._authority = (
            authority
            if authority is not None
            else MediaLeaseAuthority(
                _application_claim, retention_seconds=RETAINED_DERIVATIVE_IDLE_SECONDS
            )
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h3-media-leases")
        self._claim = threading.Lock()
        self._claim_state = threading.Lock()
        self._active_scope: str | None = None
        self._playback_handoff = threading.Lock()
        self._stopped = threading.Event()

    def close(self) -> None:
        self._stopped.set()
        self._authority.close()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def claimed(self) -> bool:
        return self._claim.locked()

    def _try_claim(self, command: CreateLeaseRequest | LeaseCommand, capability: str) -> bool:
        with self._claim_state:
            if not self._claim.acquire(blocking=False):
                return False
            try:
                self._active_scope = (
                    command.scope
                    if isinstance(command, CreateLeaseRequest)
                    else self._authority.lease_scope(command, capability)
                )
            except BaseException:
                self._claim.release()
                raise
            return True

    def _release_claim(self) -> None:
        with self._claim_state:
            self._active_scope = None
            self._claim.release()

    def _active_decoration(self) -> bool:
        with self._claim_state:
            return self._active_scope == "asset"

    async def _acquire_claim(
        self,
        command: CreateLeaseRequest | LeaseCommand,
        request: Any,
        deadline: float,
        capability: str,
    ) -> str | None:
        if not self._playback_handoff.locked() and self._try_claim(command, capability):
            return None
        if (
            not isinstance(command, CreateLeaseRequest)
            or command.scope != "clip"
            or not self._active_decoration()
            or not self._playback_handoff.acquire(blocking=False)
        ):
            return "busy"
        try:
            handoff_deadline = min(deadline, time.monotonic() + _PLAYBACK_HANDOFF_SECONDS)
            # IMPORTANT: a disconnected asset create keeps the native no-queue claim until its
            # worker acknowledges cancellation. Admit one clip waiter only and join that actual
            # claim release; returning busy here defeats browser-side playback preemption.
            while time.monotonic() < handoff_deadline:
                if self._closing(request):
                    return "cancelled"
                if self._stopped.is_set():
                    return "lease_gone"
                if self._try_claim(command, capability):
                    return None
                await asyncio.sleep(0.05)
            return "busy"
        finally:
            self._playback_handoff.release()

    @staticmethod
    def _closing(request: Any) -> bool:
        transport = getattr(request, "transport", None)
        return transport is None or bool(transport.is_closing())

    @staticmethod
    def _json(payload: dict[str, object], status: int = 200, capability: str = "") -> Any:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > MAX_CONTROL_BYTES:
            raise MediaLeaseError("internal_failure")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if capability:
            headers[CAPABILITY_HEADER] = capability
        return encoded_response(encoded, status=status, headers=headers)

    @staticmethod
    def _geometry_header(value: object) -> str:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("ascii")) > _MAX_GEOMETRY_HEADER_BYTES:
            raise MediaLeaseError("internal_failure")
        return encoded

    def _work(
        self,
        command: CreateLeaseRequest | LeaseCommand,
        capability: str,
        cancellation: _Cancellation,
    ) -> _Result:
        if isinstance(command, CreateLeaseRequest):
            from .comfyui_media_runtime import media_runtime_lease
            from .media_runtime_manager import MediaRuntimeBusy

            # Derivative generation runs the media tools; it fails fast during a binding
            # transition instead of running on a generator the transition is about to drop.
            try:
                with media_runtime_lease():
                    receipt, capability = self._authority.create(command, cancellation=cancellation)
            except MediaRuntimeBusy:
                raise MediaLeaseError("busy") from None
            return _Result(receipt, capability)
        if command.operation == "open":
            read = self._authority.open(command, capability, cancellation=cancellation)
            try:
                return _Result(self._authority.metadata(command, capability), capability, read)
            except BaseException:
                read.discard()
                raise
        if command.operation == "renew":
            return _Result(
                self._authority.renew(command, capability, cancellation=cancellation), capability
            )
        if command.operation == "transfer":
            receipt, capability = self._authority.transfer(
                command, capability, cancellation=cancellation
            )
            return _Result(receipt, capability)
        raise MediaLeaseError()

    def _discard(self, result: _Result) -> None:
        if result.read is not None:
            result.read.discard()
        receipt = result.receipt
        try:
            command = LeaseCommand(
                "release",
                "abandoned-response",
                str(receipt["leaseId"]),
                int(str(receipt["revision"])),
                str(receipt["ownerId"]),
                int(str(receipt["runtimeEpoch"])),
            )
            self._authority.release(command, result.capability)
        except MediaLeaseError:
            pass

    def _abandoned(self, future: Future[_Result]) -> None:
        try:
            self._discard(future.result())
        except Exception:
            return
        finally:
            self._release_claim()

    async def control(self, request: Any) -> Any:
        return await self._handle(request, opening=False)

    async def open(self, request: Any) -> Any:
        return await self._handle(request, opening=True)

    async def _handle(self, request: Any, *, opening: bool) -> Any:
        request_id: str | None = None
        deadline = time.monotonic() + 60.0
        cancellation = _Cancellation()
        future: Future[_Result] | None = None
        result: _Result | None = None
        claimed = False
        delivered = False
        response: Any = None

        def error(reason: str, status: int | None = None) -> Any:
            return self._json(
                {"schema": ERROR_SCHEMA, "requestId": request_id, "reason": reason},
                _STATUSES[reason] if status is None else status,
            )

        try:
            headers = request.headers
            # CRITICAL: origin policy belongs to the shared edge; a private copy can drift and
            # admit duplicate or foreign origins before reading a capability-bearing request. The
            # shared rule also validates `Host` against the same trusted target, so a second,
            # literal Host check here would only pin this edge to one deployment again.
            if not origin_accepted(request, OriginRule.EXACT):
                return error("invalid_request", 403)
            if request.content_type != "application/json":
                return error("invalid_request", 415)
            if (
                request.query_string
                or headers.getall("Range", [])
                or headers.getall("Content-Encoding", [])
            ):
                return error("invalid_request")
            if (
                request.content_length is not None
                and not 0 <= request.content_length <= MAX_CONTROL_BYTES
            ):
                return error("invalid_request", 413)
            body = bytearray()
            while True:
                chunk = await asyncio.wait_for(
                    request.content.read(MAX_CONTROL_BYTES + 1 - len(body)),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_CONTROL_BYTES:
                    return error("invalid_request", 413)
            command = decode_lease_request(bytes(body))
            request_id = command.request_id
            capabilities = headers.getall(CAPABILITY_HEADER, [])
            if isinstance(command, CreateLeaseRequest):
                if opening or capabilities:
                    return error("invalid_request")
                capability = ""
            else:
                if (command.operation == "open") != opening:
                    return error("invalid_request")
                if len(capabilities) != 1 or _CAPABILITY.fullmatch(capabilities[0]) is None:
                    return error("lease_gone")
                capability = capabilities[0]
            if self._stopped.is_set():
                return error("lease_gone")
            # CRITICAL: a client that has gone still gets a response object. Every host middleware
            # receives the return value (a foreign pack's CSP middleware reads `response.headers`)
            # and aiohttp logs a missing return as a server error; its write to the closed
            # transport fails silently. Never `return None` because nobody is listening.
            if self._closing(request):
                return error("cancelled")
            if isinstance(command, LeaseCommand) and command.operation == "release":
                return self._json(self._authority.release(command, capability))
            acquisition_error = await self._acquire_claim(command, request, deadline, capability)
            if acquisition_error is not None:
                return error(acquisition_error)
            claimed = True
            future = self._executor.submit(self._work, command, capability, cancellation)
            completion = asyncio.wrap_future(future)
            # IMPORTANT: an abandoned worker can still fail. Consume the wrapper exception;
            # the existing result/discard paths retain ownership of the worker and its claim.
            completion.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
            while not future.done():
                if self._closing(request):
                    return error("cancelled")
                if self._stopped.is_set():
                    return error("lease_gone")
                if time.monotonic() >= deadline:
                    return error("timeout")
                # IMPORTANT: wake on completion, not after a fixed polling sleep. Keep the
                # observation bound without cancelling a worker whose claim must survive timeout.
                await asyncio.wait((completion,), timeout=0.05)
            result = future.result()
            if self._closing(request):
                return error("cancelled")
            if self._stopped.is_set():
                return error("lease_gone")
            if time.monotonic() >= deadline:
                return error("timeout")
            if result.read is None:
                rotated = isinstance(command, CreateLeaseRequest) or command.operation == "transfer"
                response = self._json(
                    result.receipt, capability=result.capability if rotated else ""
                )
            else:
                metadata = result.receipt
                response = streamed_response(
                    status=200,
                    headers={
                        "Content-Type": str(metadata["mediaType"]),
                        "Content-Length": str(metadata["byteCount"]),
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "X-H3-Context-Media-Lease-Revision": str(metadata["revision"]),
                        "X-H3-Context-Media-Derivative": str(metadata["derivativeFingerprint"]),
                        GEOMETRY_HEADER: self._geometry_header(metadata["geometry"]),
                    },
                )
                await asyncio.wait_for(
                    response.prepare(request), max(0.001, deadline - time.monotonic())
                )
                while True:
                    chunk = result.read.read_chunk()
                    if not chunk:
                        break
                    await asyncio.wait_for(
                        response.write(chunk), max(0.001, deadline - time.monotonic())
                    )
                await asyncio.wait_for(
                    response.write_eof(), max(0.001, deadline - time.monotonic())
                )
                result.read.discard()
            delivered = True
            return response
        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            reason = "timeout"
        except MediaLeaseError as exc:
            reason = exc.reason
        except Exception:
            reason = "internal_failure"
        finally:
            cancellation.event.set()
            if claimed:
                # CRITICAL: disconnect cannot release the no-queue claim while native work is
                # still running; late output must be revoked before admitting another build.
                if future is not None and not future.done():
                    future.add_done_callback(self._abandoned)
                else:
                    if result is None and future is not None:
                        try:
                            result = future.result()
                        except Exception:
                            result = None
                    if result is not None and not delivered:
                        self._discard(result)
                    self._release_claim()
        if response is not None and response.prepared:
            transport = getattr(request, "transport", None)
            if transport is not None:
                transport.close()
            return response
        return error(reason)


_SERVICE: MediaLeaseRouteService | None = None


def authoring_media_lease_generation_busy() -> bool:
    """A lease operation holds the no-queue claim until its worker exits."""

    service = _SERVICE
    return service is not None and service.claimed()


def ensure_authoring_media_lease_routes_registered() -> bool:
    found = host_web_and_routes()
    if found is None:
        return False
    routes = cast(_RouteTable, found[1])
    paths = (LEASE_ROUTE, LEASE_ROUTE + "/open")
    existing = [route for route in routes if getattr(route, "path", None) in paths]
    if existing:
        return (
            len(existing) == 2
            and {getattr(route, "path", None) for route in existing} == set(paths)
            and all(
                getattr(route, "method", None) == "POST"
                and getattr(getattr(route, "handler", None), _OWNER, None) == REQUEST_SCHEMA
                and getattr(getattr(route, "handler", None), "__module__", None) == __name__
                for route in existing
            )
        )
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MediaLeaseRouteService()
        atexit.register(_SERVICE.close)
    service = _SERVICE

    # CRITICAL: explicit decorators keep both streamed lease routes visible to the
    # architecture inventory; loop registration silently omits their responsibility rows.
    @routes.post(LEASE_ROUTE)
    async def control(request: Any) -> Any:
        return await service.control(request)

    @routes.post("/h3-context/v1/authoring/media-source-leases/open")
    async def opening(request: Any) -> Any:
        return await service.open(request)

    for handler in (control, opening):
        setattr(handler, _OWNER, REQUEST_SCHEMA)
    return True
