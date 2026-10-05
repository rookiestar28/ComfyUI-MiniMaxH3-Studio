"""M25-02 bounded Authoring source-preview route and explicit runtime seam."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.authoring_preview_protocol import (
    AUTHORING_PREVIEW_ERROR_SCHEMA,
    AUTHORING_PREVIEW_REQUEST_SCHEMA,
    MAX_AUTHORING_PREVIEW_REQUEST_BYTES,
    AuthoringPreviewErrorReason,
    decode_authoring_preview_request_json,
)
from .authoring_source_binding import AuthoringSourceBindingError
from .av_reconstruction_media import AVMediaAdapterError, QualifiedAVMediaAdapter
from .comfyui_authoring_workspace import (
    AuthoringWorkbenchError,
    admit_authoring_media_preview,
    authoring_media_preview_is_current,
)
from .comfyui_route_seam import OriginRule, origin_accepted
from .media_preview_authority import MAX_MEDIA_PREVIEW_RESPONSE_BYTES

AUTHORING_MEDIA_PREVIEW_ROUTE = "/h3-context/v1/authoring/media-preview"
AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER = "X-H3-Context-Embedded-Audio"
AUTHORING_MEDIA_PREVIEW_FILENAME = 'inline; filename="h3-authoring-preview.mp4"'

AuthoringAudioDisposition = Literal["present_bound", "absent", "unavailable"]
_AUDIO_DISPOSITIONS = frozenset({"present_bound", "absent", "unavailable"})
_ADAPTER_LOCK = threading.Lock()
_ADAPTER: QualifiedAVMediaAdapter | None = None
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_authoring_media_preview_v1__"
_ROUTE_REGISTERED = False
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_EXECUTION_CLAIM = threading.Lock()
_WORK_DEADLINE_SECONDS = 43.0
_REQUEST_DEADLINE_SECONDS = 60.0
_POLL_SECONDS = 0.05


class _Cancellation:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class AuthoringMediaPreviewResult:
    _body: bytes | bytearray | None = field(repr=False)
    audio_disposition: AuthoringAudioDisposition
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self._body, (bytes, bytearray))
            or not 1 <= len(self._body) <= MAX_MEDIA_PREVIEW_RESPONSE_BYTES
            or self.audio_disposition not in _AUDIO_DISPOSITIONS
        ):
            raise ValueError("authoring preview result is invalid")

    def take(self) -> bytes | bytearray:
        with self._lock:
            if self._body is None:
                raise RuntimeError("authoring preview result was already consumed")
            body = self._body
            self._body = None
            return body

    def discard(self) -> None:
        with self._lock:
            self._body = None


def publish_authoring_media_preview_adapter(adapter: QualifiedAVMediaAdapter) -> None:
    """Publish one caller-owned exact adapter without exposing any executable locator."""

    if type(adapter) is not QualifiedAVMediaAdapter:
        raise TypeError("qualified authoring preview adapter is invalid")
    global _ADAPTER
    with _ADAPTER_LOCK:
        if _ADAPTER is not None and _ADAPTER is not adapter:
            raise RuntimeError("qualified authoring preview adapter is already published")
        _ADAPTER = adapter


def current_authoring_media_preview_adapter() -> QualifiedAVMediaAdapter | None:
    with _ADAPTER_LOCK:
        return _ADAPTER


def authoring_media_preview_busy() -> bool:
    """A preview job holds the route claim until its worker exits."""

    return _EXECUTION_CLAIM.locked()


def clear_authoring_media_preview_adapter(
    expected: QualifiedAVMediaAdapter | None = None,
) -> bool:
    global _ADAPTER
    with _ADAPTER_LOCK:
        if expected is None or _ADAPTER is not expected:
            return False
        _ADAPTER = None
        return True


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="h3-authoring-preview")
        return _EXECUTOR


def _transport_is_closing(request: object) -> bool:
    transport = getattr(request, "transport", None)
    checker = getattr(transport, "is_closing", None)
    try:
        return bool(checker()) if callable(checker) else transport is None
    except Exception:
        return True


def _error_status(reason: AuthoringPreviewErrorReason) -> int:
    return {
        AuthoringPreviewErrorReason.INVALID_REQUEST: 400,
        AuthoringPreviewErrorReason.AUTHORITY_MISMATCH: 404,
        AuthoringPreviewErrorReason.STALE: 409,
        AuthoringPreviewErrorReason.UNSUPPORTED: 422,
        AuthoringPreviewErrorReason.SOURCE_TOO_LARGE: 413,
        AuthoringPreviewErrorReason.SOURCE_TOO_LONG: 422,
        AuthoringPreviewErrorReason.BUSY: 429,
        AuthoringPreviewErrorReason.CANCELLED: 499,
        AuthoringPreviewErrorReason.TIMEOUT: 504,
        AuthoringPreviewErrorReason.CONVERSION_FAILED: 422,
        AuthoringPreviewErrorReason.INTERNAL_FAILURE: 500,
    }[reason]


def _workspace_reason(exc: AuthoringWorkbenchError) -> AuthoringPreviewErrorReason:
    if exc.code in {"preview_clip_unknown", "preview_authority_mismatch", "workspace_unavailable"}:
        return AuthoringPreviewErrorReason.AUTHORITY_MISMATCH
    if exc.code in {"preview_stale", "workspace_gone"}:
        return AuthoringPreviewErrorReason.STALE
    if exc.code == "preview_source_too_long":
        return AuthoringPreviewErrorReason.SOURCE_TOO_LONG
    if exc.code == "preview_request_invalid":
        return AuthoringPreviewErrorReason.INVALID_REQUEST
    return AuthoringPreviewErrorReason.UNSUPPORTED


def _adapter_reason(exc: Exception) -> AuthoringPreviewErrorReason:
    code = getattr(exc, "code", "")
    if code in {"cancelled", "preview_cancelled"}:
        return AuthoringPreviewErrorReason.CANCELLED
    if code in {"preview_deadline", "preview_timeout", "process_timeout"}:
        return AuthoringPreviewErrorReason.TIMEOUT
    if code in {"preview_busy", "resource_limit", "media_runtime_busy"}:
        return AuthoringPreviewErrorReason.BUSY
    if code == "preview_source_too_large":
        return AuthoringPreviewErrorReason.SOURCE_TOO_LARGE
    if code == "preview_source_too_long":
        return AuthoringPreviewErrorReason.SOURCE_TOO_LONG
    if code in {"adapter_unavailable", "preview_source_unsupported", "source_unsupported"}:
        return AuthoringPreviewErrorReason.UNSUPPORTED
    if code in {"source_stale", "inspection_stale"}:
        return AuthoringPreviewErrorReason.STALE
    if code in {"cleanup_failed", "media_output_invalid"}:
        return AuthoringPreviewErrorReason.INTERNAL_FAILURE
    return AuthoringPreviewErrorReason.CONVERSION_FAILED


def _render(
    claim: object,
    adapter: QualifiedAVMediaAdapter,
    deadline: float,
    cancellation: _Cancellation,
) -> AuthoringMediaPreviewResult:
    from .comfyui_media_runtime import media_runtime_lease

    render = getattr(claim, "render", None)
    if not callable(render):
        raise AuthoringSourceBindingError("source_unsupported")
    # IMPORTANT: the lease is held on the worker for exactly the media execution, and the adapter
    # read on the event loop is rechecked inside it. A transition that replaced the binding between
    # that read and this worker would otherwise run the preview on a detached adapter.
    with media_runtime_lease():
        if current_authoring_media_preview_adapter() is not adapter:
            raise AuthoringSourceBindingError("adapter_unavailable")
        body, audio = render(adapter, deadline=deadline, cancellation=cancellation)
    return AuthoringMediaPreviewResult(body, audio)


def _finish_abandoned(future: Future[AuthoringMediaPreviewResult]) -> None:
    """Dispose late worker output before releasing the no-queue route claim."""

    try:
        result = future.result()
    except Exception:
        result = None
    if result is not None:
        result.discard()
    _EXECUTION_CLAIM.release()


def ensure_authoring_media_preview_route_registered() -> bool:
    """Lazily own the Authoring-only POST route; runtime publication remains separate."""

    from .media_runtime_manager import MediaRuntimeBusy

    global _ROUTE_REGISTERED
    server_module = sys.modules.get("server")
    aiohttp_module = sys.modules.get("aiohttp")
    prompt_server = getattr(server_module, "PromptServer", None)
    web = getattr(aiohttp_module, "web", None)
    routes = getattr(getattr(prompt_server, "instance", None), "routes", None)
    if routes is None or web is None:
        return False
    for route in routes:
        if (
            getattr(route, "method", None) == "POST"
            and getattr(route, "path", None) == AUTHORING_MEDIA_PREVIEW_ROUTE
        ):
            handler = getattr(route, "handler", None)
            owned = (
                callable(handler)
                and getattr(handler, _ROUTE_OWNER_ATTRIBUTE, None)
                == AUTHORING_PREVIEW_REQUEST_SCHEMA
                and getattr(handler, "__module__", None) == __name__
            )
            _ROUTE_REGISTERED = owned
            return owned

    @routes.post(AUTHORING_MEDIA_PREVIEW_ROUTE)
    async def authoring_media_preview(request):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        work_deadline = started + _WORK_DEADLINE_SECONDS
        request_deadline = started + _REQUEST_DEADLINE_SECONDS
        request_id: str | None = None

        def error(reason: AuthoringPreviewErrorReason, status: int | None = None) -> Any:
            encoded = json.dumps(
                {
                    "schema": AUTHORING_PREVIEW_ERROR_SCHEMA,
                    "requestId": request_id,
                    "reason": reason.value,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return web.Response(
                body=encoded,
                status=_error_status(reason) if status is None else status,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        if request.content_type != "application/json":
            return error(AuthoringPreviewErrorReason.INVALID_REQUEST, 415)
        headers = getattr(request, "headers", None)
        getall = getattr(headers, "getall", None)
        ranges = getall("Range", []) if callable(getall) else []
        # CRITICAL: admission is the shared, deployment-aware rule. A private origin constant here
        # refused every deployment but one, and drifted from the seam without any test noticing.
        if not origin_accepted(request, OriginRule.EXACT):
            return error(AuthoringPreviewErrorReason.INVALID_REQUEST, 403)
        if ranges or getattr(request, "query_string", ""):
            return error(AuthoringPreviewErrorReason.INVALID_REQUEST)
        content_length = request.content_length
        if content_length is not None and content_length > MAX_AUTHORING_PREVIEW_REQUEST_BYTES:
            return error(AuthoringPreviewErrorReason.INVALID_REQUEST, 413)
        try:
            body = bytearray()
            while True:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    return error(AuthoringPreviewErrorReason.TIMEOUT)
                chunk = await asyncio.wait_for(
                    request.content.read(MAX_AUTHORING_PREVIEW_REQUEST_BYTES + 1 - len(body)),
                    timeout=remaining,
                )
                if type(chunk) is not bytes:
                    raise ValueError("authoring preview reader returned non-bytes")
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_AUTHORING_PREVIEW_REQUEST_BYTES:
                    return error(AuthoringPreviewErrorReason.INVALID_REQUEST, 413)
            decoded = decode_authoring_preview_request_json(bytes(body))
            request_id = decoded.request_id
            # CRITICAL: a client that has gone still gets a response object. Every host middleware
            # receives the return value (a foreign pack's CSP middleware reads `response.headers`)
            # and aiohttp logs a missing return as a server error; its write to the closed
            # transport fails silently. Never `return None` because nobody is listening.
            if _transport_is_closing(request):
                return error(AuthoringPreviewErrorReason.CANCELLED)
            adapter = current_authoring_media_preview_adapter()
            if adapter is None:
                from .comfyui_media_runtime import request_media_runtime_activation

                # The event loop never waits for activation; a later request finds the adapter.
                request_media_runtime_activation()
                return error(AuthoringPreviewErrorReason.UNSUPPORTED)
            claim = admit_authoring_media_preview(decoded)
        except (TypeError, ValueError):
            return error(AuthoringPreviewErrorReason.INVALID_REQUEST)
        except AuthoringWorkbenchError as exc:
            return error(_workspace_reason(exc))
        except (asyncio.TimeoutError, TimeoutError):
            return error(AuthoringPreviewErrorReason.TIMEOUT)
        except Exception:
            return error(AuthoringPreviewErrorReason.INTERNAL_FAILURE)

        if not _EXECUTION_CLAIM.acquire(blocking=False):
            return error(AuthoringPreviewErrorReason.BUSY)
        cancellation = _Cancellation()
        result: AuthoringMediaPreviewResult | None = None
        future: Future[AuthoringMediaPreviewResult] | None = None
        try:
            future = _executor().submit(_render, claim, adapter, work_deadline, cancellation)
            while not future.done():
                if _transport_is_closing(request):
                    cancellation.cancel()
                    return error(AuthoringPreviewErrorReason.CANCELLED)
                if time.monotonic() >= work_deadline:
                    cancellation.cancel()
                    return error(AuthoringPreviewErrorReason.TIMEOUT)
                await asyncio.sleep(_POLL_SECONDS)
            result = future.result()
            if _transport_is_closing(request):
                result.discard()
                return error(AuthoringPreviewErrorReason.CANCELLED)
            if time.monotonic() >= work_deadline:
                result.discard()
                return error(AuthoringPreviewErrorReason.TIMEOUT)
            if not authoring_media_preview_is_current(claim):
                result.discard()
                return error(AuthoringPreviewErrorReason.STALE)
            response_body = result.take()
            if time.monotonic() >= request_deadline:
                return error(AuthoringPreviewErrorReason.TIMEOUT)
            return web.Response(
                body=response_body,
                status=200,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(response_body)),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": AUTHORING_MEDIA_PREVIEW_FILENAME,
                    AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER: result.audio_disposition,
                },
            )
        except (AVMediaAdapterError, AuthoringSourceBindingError, MediaRuntimeBusy) as exc:
            if result is not None:
                result.discard()
            return error(_adapter_reason(exc))
        except Exception:
            if result is not None:
                result.discard()
            return error(AuthoringPreviewErrorReason.INTERNAL_FAILURE)
        finally:
            if future is not None and not future.done():
                cancellation.cancel()
                # CRITICAL: retain the route claim until the worker actually exits. Releasing it
                # here would let the single-worker executor queue a second private media job.
                future.add_done_callback(_finish_abandoned)
            else:
                _EXECUTION_CLAIM.release()

    setattr(
        authoring_media_preview,
        _ROUTE_OWNER_ATTRIBUTE,
        AUTHORING_PREVIEW_REQUEST_SCHEMA,
    )
    _ROUTE_REGISTERED = True
    return True


__all__ = [
    "AUTHORING_MEDIA_PREVIEW_AUDIO_HEADER",
    "AUTHORING_MEDIA_PREVIEW_FILENAME",
    "AUTHORING_MEDIA_PREVIEW_ROUTE",
    "AuthoringAudioDisposition",
    "AuthoringMediaPreviewResult",
    "authoring_media_preview_busy",
    "clear_authoring_media_preview_adapter",
    "current_authoring_media_preview_adapter",
    "publish_authoring_media_preview_adapter",
    "ensure_authoring_media_preview_route_registered",
]
