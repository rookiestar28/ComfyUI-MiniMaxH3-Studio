"""Bounded private aggregate preview route for the Production workbench."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

from .comfyui_production_workspace import (
    ProductionWorkbenchError,
    admit_media_preview_source,
    media_preview_source_is_current,
)
from .comfyui_route_seam import OriginRule, origin_accepted
from .media_preview_authority import (
    MAX_MEDIA_PREVIEW_RESPONSE_BYTES,
    MediaPreviewSourceAuthority,
)

MEDIA_PREVIEW_REQUEST_SCHEMA = "h3.context.production.media_preview.request.v1"
MEDIA_PREVIEW_ROUTE = "/h3-context/v1/production/media-preview"
MAX_MEDIA_PREVIEW_REQUEST_BYTES = 8_192
MAX_MEDIA_PREVIEW_REQUEST_DEPTH = 2
MAX_MEDIA_PREVIEW_REQUEST_NODES = 32

_REQUEST_KEYS = {
    "schema",
    "workspace_handle",
    "expected_workspace_revision",
    "expected_workspace_fingerprint",
    "output_handle",
}
_WORKSPACE_HANDLE = re.compile(r"pw_[A-Za-z0-9_-]{32,96}\Z")
_OUTPUT_HANDLE = re.compile(r"out_[0-9a-f]{40}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_media_preview_request_v1__"
_ROUTE_REGISTERED = False
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_EXECUTION_CLAIM = threading.Lock()
_MAX_RESPONSE_BYTES = MAX_MEDIA_PREVIEW_RESPONSE_BYTES
_WORK_DEADLINE_SECONDS = 43.0
_REQUEST_DEADLINE_SECONDS = 60.0
_ADMISSION_POLL_SECONDS = 0.05


class _AdmissionReadTimeout(Exception):
    pass


class _AdmissionTransportClosed(Exception):
    pass


class _MediaPreviewAction(TypedDict):
    schema: str
    workspace_handle: str
    expected_workspace_revision: int
    expected_workspace_fingerprint: str
    output_handle: str


class _Cancellation:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class OwnedPreviewBody:
    _body: bytes | bytearray | None = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        body = self._body
        if not isinstance(body, (bytes, bytearray)) or not 1 <= len(body) <= _MAX_RESPONSE_BYTES:
            raise ValueError("preview body is invalid")

    def take(self) -> bytes | bytearray:
        with self._lock:
            if self._body is None:
                raise RuntimeError("preview body is unavailable")
            body = self._body
            self._body = None
            return body

    def clear(self) -> None:
        with self._lock:
            self._body = None


@dataclass(slots=True)
class _ClaimOwnership:
    cancellation: _Cancellation
    future: Future[OwnedPreviewBody] | None = None
    abandoned: bool = False
    taken: bool = False
    released: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _release_locked(self) -> None:
        if not self.released:
            self.released = True
            _EXECUTION_CLAIM.release()

    def attach(self, future: Future[OwnedPreviewBody]) -> None:
        with self.lock:
            self.future = future

        def complete(completed: Future[OwnedPreviewBody]) -> None:
            with self.lock:
                failed = completed.cancelled() or completed.exception() is not None
                if failed:
                    self._release_locked()
                    return
                if self.abandoned:
                    completed.result().clear()
                    self._release_locked()

        future.add_done_callback(complete)

    def abandon(self) -> None:
        self.cancellation.cancel()
        with self.lock:
            self.abandoned = True
            future = self.future
            if future is not None and future.done():
                if not future.cancelled() and future.exception() is None:
                    future.result().clear()
                self._release_locked()

    def take(self) -> bytes | bytearray:
        with self.lock:
            if self.abandoned or self.future is None or not self.future.done():
                raise RuntimeError("preview ownership is unavailable")
            body = self.future.result().take()
            self.taken = True
            return body

    def finish(self) -> None:
        with self.lock:
            self._release_locked()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h3-preview")
        return _EXECUTOR


def _render_source(
    source: MediaPreviewSourceAuthority,
    *,
    deadline: float,
    cancellation: _Cancellation,
) -> OwnedPreviewBody:
    return OwnedPreviewBody(source.render(deadline=deadline, cancellation=cancellation))


def _abort_transport(request: object) -> None:
    transport = getattr(request, "transport", None)
    abort = getattr(transport, "abort", None)
    close = getattr(transport, "close", None)
    try:
        if callable(abort):
            abort()
        elif callable(close):
            close()
    except Exception:
        return


def _transport_is_closing(request: object) -> bool:
    transport = getattr(request, "transport", None)
    if transport is None:
        # CRITICAL: aiohttp clears the request transport once the connection is lost. Treating an
        # absent transport as open kept a disconnected render running to its work deadline while it
        # held the single preview claim, so every other preview was refused as busy.
        return True
    closing = getattr(transport, "is_closing", None)
    try:
        return bool(callable(closing) and closing())
    except Exception:
        return True


def _consume_admission_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


async def _read_admission_chunk(request: Any, limit: int, *, deadline: float) -> object:
    """Read one body chunk without letting a slow client hold the host route forever."""

    if _transport_is_closing(request):
        raise _AdmissionTransportClosed
    read_task: asyncio.Task[Any] = asyncio.create_task(request.content.read(limit))
    try:
        while True:
            if _transport_is_closing(request):
                raise _AdmissionTransportClosed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _AdmissionReadTimeout
            try:
                # CRITICAL: keep one live read task; cancelling/reissuing StreamReader.read can
                # consume or corrupt a partially delivered request while polling disconnect state.
                return await asyncio.wait_for(
                    asyncio.shield(read_task),
                    timeout=min(_ADMISSION_POLL_SECONDS, remaining),
                )
            except (TimeoutError, asyncio.TimeoutError):
                if read_task.done():
                    return read_task.result()
    finally:
        if not read_task.done():
            read_task.cancel()
            read_task.add_done_callback(_consume_admission_task)
        else:
            _consume_admission_task(read_task)


def _unsent_empty(web: Any, status: int) -> Any:
    """The owned bodiless response for a client that has gone; aiohttp's own write then fails.

    CRITICAL: a handler returns a response object on every path. Every host middleware receives the
    return value (a foreign pack's CSP middleware reads `response.headers`) and aiohttp logs a
    missing return as a server error. Never `return None` because nobody is listening.
    """

    return web.StreamResponse(
        status=status,
        headers={"Cache-Control": "no-store", "Content-Length": "0"},
    )


async def _emit_empty(web: Any, request: object, status: int, *, deadline: float) -> Any:
    """Emit the owned empty response inside the request deadline, or abort and return it."""

    response = _unsent_empty(web, status)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(response.prepare(request), timeout=remaining)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(response.write_eof(), timeout=remaining)
        return response
    except (TimeoutError, asyncio.TimeoutError, OSError, RuntimeError):
        _abort_transport(request)
        return response


def _worker_failure_status(exception: BaseException) -> int:
    code = getattr(exception, "code", None)
    if code in {"preview_deadline", "timed_out"}:
        return 504
    if code in {
        "adapter_capability_changed",
        "adapter_unavailable",
        "media_output_invalid",
        "media_process_failed",
        "member_too_large",
        "preview_copy_unavailable",
        "preview_source_unavailable",
        "read_limit",
        "reconstruction_not_complete",
        "resource_limit",
        "store_entry_unavailable",
        "unsafe_store_entry",
    }:
        return 422
    return 500


async def _settle_worker(wrapped: asyncio.Future[OwnedPreviewBody], deadline: float) -> None:
    try:
        await asyncio.wait_for(
            asyncio.shield(wrapped),
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except asyncio.CancelledError:
        return
    except Exception:
        return


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _shape(value: object, *, depth: int = 0) -> int:
    if depth > MAX_MEDIA_PREVIEW_REQUEST_DEPTH:
        raise ValueError("preview request exceeds the depth bound")
    if type(value) is dict:
        count = 1
        for key, item in value.items():
            if type(key) is not str or len(key) > 192:
                raise ValueError("preview request key is invalid")
            count += 1 + _shape(item, depth=depth + 1)
        return count
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 192:
            raise ValueError("preview request text exceeds its bound")
        return 1
    raise ValueError("preview request contains an unsupported value")


def decode_media_preview_request_json(data: bytes) -> _MediaPreviewAction:
    """Decode one strict, content-free preview identity request."""

    if type(data) is not bytes or len(data) > MAX_MEDIA_PREVIEW_REQUEST_BYTES:
        raise ValueError("preview request body is invalid")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("preview request JSON is invalid") from exc
    if _shape(value) > MAX_MEDIA_PREVIEW_REQUEST_NODES:
        raise ValueError("preview request exceeds the node bound")
    if type(value) is not dict or set(value) != _REQUEST_KEYS:
        raise ValueError("preview request must be one closed object")
    if value["schema"] != MEDIA_PREVIEW_REQUEST_SCHEMA:
        raise ValueError("preview request schema is unsupported")
    if (
        type(value["workspace_handle"]) is not str
        or _WORKSPACE_HANDLE.fullmatch(value["workspace_handle"]) is None
    ):
        raise ValueError("workspace_handle is invalid")
    revision = value["expected_workspace_revision"]
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        raise ValueError("expected_workspace_revision is invalid")
    if (
        type(value["expected_workspace_fingerprint"]) is not str
        or _FINGERPRINT.fullmatch(value["expected_workspace_fingerprint"]) is None
    ):
        raise ValueError("expected_workspace_fingerprint is invalid")
    if (
        type(value["output_handle"]) is not str
        or _OUTPUT_HANDLE.fullmatch(value["output_handle"]) is None
    ):
        raise ValueError("output_handle is invalid")
    return cast(_MediaPreviewAction, value)


def ensure_media_preview_route_registered() -> bool:
    """Lazily register the owned POST route without importing optional host modules."""

    global _ROUTE_REGISTERED
    # CRITICAL: optional host integration must never become an import-time aiohttp dependency.
    server_module = sys.modules.get("server")
    aiohttp_module = sys.modules.get("aiohttp")
    prompt_server = getattr(server_module, "PromptServer", None)
    web = getattr(aiohttp_module, "web", None)
    instance = getattr(prompt_server, "instance", None)
    routes = getattr(instance, "routes", None)
    if routes is None or web is None:
        return False
    for route in routes:
        if (
            getattr(route, "method", None) == "POST"
            and getattr(route, "path", None) == MEDIA_PREVIEW_ROUTE
        ):
            handler = getattr(route, "handler", None)
            owned = (
                callable(handler)
                and getattr(handler, _ROUTE_OWNER_ATTRIBUTE, None) == MEDIA_PREVIEW_REQUEST_SCHEMA
                and getattr(handler, "__module__", None) == __name__
            )
            _ROUTE_REGISTERED = owned
            return owned

    @routes.post(MEDIA_PREVIEW_ROUTE)
    async def media_preview(request):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        request_deadline = started + _REQUEST_DEADLINE_SECONDS
        work_deadline = started + _WORK_DEADLINE_SECONDS

        async def empty(status: int) -> Any:
            return await _emit_empty(web, request, status, deadline=request_deadline)

        if request.content_type != "application/json":
            return await empty(415)
        # CRITICAL: admission is the shared, deployment-aware rule. A private origin constant here
        # refused every deployment but one, and drifted from the seam without any test noticing.
        if not origin_accepted(request, OriginRule.EXACT):
            return await empty(403)
        headers = getattr(request, "headers", None)
        getall = getattr(headers, "getall", None)
        ranges = getall("Range", []) if callable(getall) else []
        if ranges or getattr(request, "query_string", ""):
            return await empty(400)
        content_length = request.content_length
        if content_length is not None and content_length > MAX_MEDIA_PREVIEW_REQUEST_BYTES:
            return await empty(413)
        try:
            body = bytearray()
            while True:
                chunk = await _read_admission_chunk(
                    request,
                    MAX_MEDIA_PREVIEW_REQUEST_BYTES + 1 - len(body),
                    deadline=work_deadline,
                )
                if type(chunk) is not bytes:
                    raise ValueError("preview request body reader returned a non-bytes chunk")
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_MEDIA_PREVIEW_REQUEST_BYTES:
                    return await empty(413)
            if _transport_is_closing(request):
                return _unsent_empty(web, 499)
            if time.monotonic() >= work_deadline:
                return await empty(504)
            action = decode_media_preview_request_json(bytes(body))
            if time.monotonic() >= work_deadline:
                return await empty(504)
            source = admit_media_preview_source(
                workspace_handle=str(action["workspace_handle"]),
                expected_workspace_revision=action["expected_workspace_revision"],
                expected_workspace_fingerprint=str(action["expected_workspace_fingerprint"]),
                output_handle=str(action["output_handle"]),
            )
            if _transport_is_closing(request):
                return _unsent_empty(web, 499)
            if time.monotonic() >= work_deadline:
                return await empty(504)
        except _AdmissionReadTimeout:
            return await empty(504)
        except _AdmissionTransportClosed:
            return _unsent_empty(web, 499)
        except ProductionWorkbenchError as exc:
            return await empty(exc.status)
        except (TypeError, ValueError):
            return await empty(400)
        except Exception:
            return await empty(500)

        if not _EXECUTION_CLAIM.acquire(blocking=False):
            return await empty(423)
        cancellation = _Cancellation()
        ownership = _ClaimOwnership(cancellation)
        if _transport_is_closing(request):
            ownership.finish()
            return _unsent_empty(web, 499)
        if time.monotonic() >= work_deadline:
            ownership.finish()
            return await empty(504)
        try:
            future = _executor().submit(
                _render_source,
                source,
                deadline=work_deadline,
                cancellation=cancellation,
            )
        except Exception:
            ownership.finish()
            return await empty(500)
        ownership.attach(future)
        wrapped = asyncio.wrap_future(future)
        response: Any = None
        try:
            while not wrapped.done():
                if time.monotonic() >= work_deadline:
                    ownership.abandon()
                    await _settle_worker(wrapped, started + 58.0)
                    return await empty(504)
                if _transport_is_closing(request):
                    ownership.abandon()
                    await _settle_worker(wrapped, request_deadline)
                    return _unsent_empty(web, 499)
                await asyncio.wait({wrapped}, timeout=0.05)
            if future.cancelled():
                return await empty(500)
            exception = wrapped.exception()
            if exception is not None:
                return await empty(_worker_failure_status(exception))
            if _transport_is_closing(request):
                ownership.abandon()
                return _unsent_empty(web, 499)
            if time.monotonic() >= work_deadline:
                ownership.abandon()
                return await empty(504)
            try:
                source_is_current = media_preview_source_is_current(
                    workspace_handle=str(action["workspace_handle"]),
                    expected_workspace_revision=action["expected_workspace_revision"],
                    expected_workspace_fingerprint=str(action["expected_workspace_fingerprint"]),
                    source=source,
                )
            except ProductionWorkbenchError as exc:
                ownership.abandon()
                return await empty(exc.status)
            if _transport_is_closing(request):
                ownership.abandon()
                return _unsent_empty(web, 499)
            if time.monotonic() >= work_deadline:
                ownership.abandon()
                return await empty(504)
            if not source_is_current:
                ownership.abandon()
                return await empty(409)
            response_body = ownership.take()
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(response_body)),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'inline; filename="h3-preview.mp4"',
                },
            )
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                ownership.abandon()
                return await empty(504)
            await asyncio.wait_for(response.prepare(request), timeout=remaining)
            remaining = request_deadline - time.monotonic()
            await asyncio.wait_for(response.write(response_body), timeout=max(0.0, remaining))
            remaining = request_deadline - time.monotonic()
            await asyncio.wait_for(response.write_eof(), timeout=max(0.0, remaining))
            return response
        except asyncio.CancelledError:
            ownership.abandon()
            await _settle_worker(wrapped, request_deadline)
            _abort_transport(request)
            raise
        except Exception:
            ownership.abandon()
            _abort_transport(request)
            # The transport is aborted, so returning the attempt cannot complete a truncated body.
            return response if response is not None else _unsent_empty(web, 500)
        finally:
            if ownership.taken:
                ownership.finish()

    setattr(media_preview, _ROUTE_OWNER_ATTRIBUTE, MEDIA_PREVIEW_REQUEST_SCHEMA)
    _ROUTE_REGISTERED = True
    return True


__all__ = [
    "MAX_MEDIA_PREVIEW_REQUEST_BYTES",
    "MAX_MEDIA_PREVIEW_REQUEST_DEPTH",
    "MAX_MEDIA_PREVIEW_REQUEST_NODES",
    "MEDIA_PREVIEW_REQUEST_SCHEMA",
    "MEDIA_PREVIEW_ROUTE",
    "OwnedPreviewBody",
    "decode_media_preview_request_json",
    "ensure_media_preview_route_registered",
]
