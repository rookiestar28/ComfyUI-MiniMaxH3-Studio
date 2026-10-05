"""Bounded final-output HTTP transport; no host, runtime or thread starts on import."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol, TypeVar

from ..core.authoring_output_protocol import (
    OUTPUT_CAPABILITY_PATH,
    OUTPUT_REQUEST_BYTES,
    OUTPUT_RESPONSE_SECONDS,
    OutputProtocolError,
    decode_output_cancel_json,
    decode_output_create_json,
    output_capability_wire,
    require_output_handle,
    require_output_workspace,
)
from .authoring_output_service import AuthoringOutputRegistry, OutputResponseLease
from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    already_owned,
    content_length_verdict,
    origin_accepted,
    read_bounded_body,
    route_method_available,
)

AUTHORING_RENDER_ROUTE = "/h3-context/v1/authoring/render"
AUTHORING_OUTPUT_ROUTE = "/h3-context/v1/authoring/output"
AUTHORING_OUTPUT_CAPABILITY_ROUTE = OUTPUT_CAPABILITY_PATH
_OWNER = "h3.authoring.output_status.v1"
_OWNER_ATTRIBUTE = "__h3_context_authoring_output_v1__"
_JSON_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_T = TypeVar("_T")


class _RouteTable(Protocol):
    def __iter__(self) -> Iterator[Any]: ...

    def post(
        self, path: str
    ) -> Callable[[Callable[[Any], Awaitable[Any]]], Callable[[Any], Awaitable[Any]]]: ...

    def get(
        self, path: str
    ) -> Callable[[Callable[[Any], Awaitable[Any]]], Callable[[Any], Awaitable[Any]]]: ...


class _Admission:
    """A request and any abandoned worker share one nonwaiting capacity slot."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._references = 1
        self._lock = threading.Lock()

    def retain(self) -> None:
        with self._lock:
            self._references += 1

    def release(self) -> None:
        with self._lock:
            self._references -= 1
            if self._references == 0:
                self._release()


class AuthoringOutputRoutes:
    """Explicit HTTP owner, borrowing rather than cancelling the render service."""

    def __init__(
        self,
        registry: Callable[[], AuthoringOutputRegistry | None],
    ) -> None:
        self._registry = registry
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)
        self._closed = False

    def _admit(self) -> _Admission:
        with self._lock:
            if self._closed:
                raise OutputProtocolError("runtime_unavailable")
            if not self._slots.acquire(blocking=False):
                raise OutputProtocolError("resource_limit")
            return _Admission(self._slots.release)

    async def _work(
        self, admission: _Admission, operation: Callable[[], _T], *, deadline: float
    ) -> _T:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutputProtocolError("expired")
        with self._lock:
            if self._closed:
                raise OutputProtocolError("runtime_unavailable")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="h3-output")
            admission.retain()

            def run() -> _T:
                try:
                    return operation()
                finally:
                    admission.release()

            try:
                pending = self._executor.submit(run)
            except BaseException:
                admission.release()
                raise
        wrapped = asyncio.wrap_future(pending)
        # IMPORTANT: a disconnected status reader cannot cancel an accepted render job or
        # release its worker slot early. Dispose late byte leases even after its loop closes.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), remaining)
        except BaseException:

            def discard(done: Future[_T]) -> None:
                try:
                    result = done.result()
                    if isinstance(result, OutputResponseLease):
                        result.close()
                except BaseException:
                    return

            pending.add_done_callback(discard)
            raise

    @staticmethod
    def _header(request: Any, name: str) -> str | None:
        values = request.headers.getall(name, [])
        if type(values) is not list or len(values) > 1:
            raise OutputProtocolError()
        if values and type(values[0]) is not str:
            raise OutputProtocolError()
        return values[0] if values else None

    @staticmethod
    def _workspace(request: Any) -> str:
        if list(request.query.keys()) != ["workspace_handle"]:
            raise OutputProtocolError()
        values = request.query.getall("workspace_handle", [])
        if type(values) is not list or len(values) != 1:
            raise OutputProtocolError()
        return require_output_workspace(values[0])

    @staticmethod
    def _connected(request: Any) -> bool:
        transport = getattr(request, "transport", None)
        return transport is not None and not transport.is_closing()

    async def _stream(
        self,
        request: Any,
        web: Any,
        lease: OutputResponseLease,
        admission: _Admission,
        *,
        deadline: float,
    ) -> Any:
        response = web.StreamResponse(status=lease.selection.status, headers=lease.headers)
        iterator = lease.chunks()

        async def write(operation: Any) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._connected(request):
                operation.close()
                raise OutputProtocolError("expired")
            return await asyncio.wait_for(operation, min(10.0, remaining))

        try:
            await write(response.prepare(request))
            while True:
                block = await self._work(admission, lambda: next(iterator, None), deadline=deadline)
                if block is None:
                    break
                await write(response.write(block))
            await write(response.write_eof())
            return response
        except Exception:
            # CRITICAL: after headers an interrupted body is a failed stream, never a second
            # JSON response or a successful truncated download with the original length. Abort
            # first, then hand the host this same response: raising would print a handler
            # traceback every time a media element abandons a range read, and a foreign pack's
            # middleware still receives the return value.
            transport = getattr(request, "transport", None)
            if transport is not None:
                transport.abort()
            return response
        except BaseException:
            transport = getattr(request, "transport", None)
            if transport is not None:
                transport.abort()
            raise
        finally:
            lease.close()

    async def _handle(self, kind: str, request: Any, web: Any) -> Any:
        admission: _Admission | None = None
        streaming = False
        deadline = time.monotonic() + OUTPUT_RESPONSE_SECONDS
        abandoned = threading.Event()
        try:
            post = kind in {"create", "cancel"}
            rule = OriginRule.EXACT if post else OriginRule.SAME_ORIGIN_GET
            if not origin_accepted(request, rule) or (
                post and not origin_accepted(request, OriginRule.SAME_ORIGIN_GET)
            ):
                raise OutputProtocolError("forbidden")
            # CRITICAL: ComfyUI's /api clone cannot carry allow_head=False route kwargs.
            # Refuse its implicit HEAD after origin checks, before admission/private reads.
            if request.method != ("POST" if post else "GET"):
                raise OutputProtocolError()
            if kind == "capability":
                # M25-16: a bodiless, unauthenticated-by-workspace read of the closed
                # capability projection. It takes no admission slot and touches no job.
                if request.query or request.can_read_body:
                    raise OutputProtocolError()
                return web.json_response(
                    output_capability_wire(supported=self._registry() is not None),
                    headers=_JSON_HEADERS,
                )
            admission = self._admit()
            range_header = self._header(request, "Range")
            if kind not in {"download", "preview"} and range_header is not None:
                raise OutputProtocolError()
            if post:
                if request.query or request.content_type != "application/json":
                    raise OutputProtocolError()
                if content_length_verdict(request, OUTPUT_REQUEST_BYTES) != "accepted":
                    raise OutputProtocolError()
                raw = await asyncio.wait_for(read_bounded_body(request, OUTPUT_REQUEST_BYTES), 10.0)
                if raw is None:
                    raise OutputProtocolError()
                public = decode_output_create_json(raw) if kind == "create" else None
                workspace = (
                    public.workspace_handle
                    if public is not None
                    else decode_output_cancel_json(raw)
                )
            else:
                if request.can_read_body or request.content_length not in (None, 0):
                    raise OutputProtocolError()
                workspace = self._workspace(request)
                public = None
            registry = self._registry()
            if registry is None:
                raise OutputProtocolError("runtime_unavailable")
            if kind == "create":
                if public is None:
                    raise OutputProtocolError()
                result = await self._work(
                    admission, lambda: registry.create(public), deadline=deadline
                )
            else:
                media = kind in {"preview", "download"}
                handle = require_output_handle(
                    request.match_info.get("handle"), kind="output" if media else "job"
                )
                if media:
                    if kind == "preview":
                        lease = await self._work(
                            admission,
                            lambda: registry.open_preview(
                                handle,
                                workspace,
                                range_header=range_header,
                                # CRITICAL: ComfyUI does not enable aiohttp handler cancellation.
                                # Observe the connection too, or a closed preview keeps its child
                                # running until timeout; this never cancels the final render job.
                                cancelled=lambda: (
                                    abandoned.is_set() or not self._connected(request)
                                ),
                            ),
                            deadline=deadline,
                        )
                    else:
                        lease = await self._work(
                            admission,
                            lambda: registry.open_download(
                                handle, workspace, range_header=range_header
                            ),
                            deadline=deadline,
                        )
                    streaming = True
                    return await self._stream(request, web, lease, admission, deadline=deadline)
                operation = registry.cancel if kind == "cancel" else registry.status
                result = await self._work(
                    admission, lambda: operation(handle, workspace), deadline=deadline
                )
            return web.json_response(result, headers=_JSON_HEADERS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if streaming:
                # `_stream` answers every interruption with its aborted response; anything that
                # still escapes after headers is a real defect and must not become a JSON refusal.
                raise
            refusal = (
                error
                if type(error) is OutputProtocolError
                else OutputProtocolError("internal_error")
            )
            return web.json_response(
                refusal.to_wire(), status=refusal.status, headers=_JSON_HEADERS
            )
        finally:
            abandoned.set()
            if admission is not None:
                admission.release()

    def register(self, web: Any, routes: _RouteTable) -> bool:
        rows = (
            ("capability", "GET", AUTHORING_OUTPUT_CAPABILITY_ROUTE),
            ("create", "POST", AUTHORING_RENDER_ROUTE),
            ("status", "GET", AUTHORING_RENDER_ROUTE + "/{handle}"),
            ("cancel", "POST", AUTHORING_RENDER_ROUTE + "/{handle}/cancel"),
            ("preview", "GET", AUTHORING_OUTPUT_ROUTE + "/{handle}/preview"),
            ("download", "GET", AUTHORING_OUTPUT_ROUTE + "/{handle}/download"),
        )
        policies = tuple(
            RoutePolicy(
                path=path,
                owner=_OWNER,
                owner_attribute=_OWNER_ATTRIBUTE,
                method=method,
                max_bytes=OUTPUT_REQUEST_BYTES if method == "POST" else None,
                origin=OriginRule.EXACT if method == "POST" else OriginRule.SAME_ORIGIN_GET,
            )
            for _kind, method, path in rows
        )
        ownership = [already_owned(routes, policy, __name__) for policy in policies]
        if any(value is False for value in ownership) or not all(
            route_method_available(routes, policy) for policy in policies
        ):
            return False
        if any(value is True for value in ownership):
            return all(value is True for value in ownership)

        # CRITICAL: explicit decorators keep all streamed output routes in the architecture
        # responsibility census; dynamic loop registration makes real public routes invisible.
        @routes.get(AUTHORING_OUTPUT_CAPABILITY_ROUTE)
        async def capability(request: Any) -> Any:
            return await self._handle("capability", request, web)

        @routes.post(AUTHORING_RENDER_ROUTE)
        async def create(request: Any) -> Any:
            return await self._handle("create", request, web)

        @routes.get("/h3-context/v1/authoring/render/{handle}")
        async def status(request: Any) -> Any:
            return await self._handle("status", request, web)

        @routes.post("/h3-context/v1/authoring/render/{handle}/cancel")
        async def cancel(request: Any) -> Any:
            return await self._handle("cancel", request, web)

        @routes.get("/h3-context/v1/authoring/output/{handle}/preview")
        async def preview(request: Any) -> Any:
            return await self._handle("preview", request, web)

        @routes.get("/h3-context/v1/authoring/output/{handle}/download")
        async def download(request: Any) -> Any:
            return await self._handle("download", request, web)

        for handler, policy in zip(
            (capability, create, status, cancel, preview, download), policies, strict=True
        ):
            setattr(handler, policy.owner_attribute, policy.owner)
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
