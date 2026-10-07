"""M17-15 lazy route ownership, empty-error and bounded response checks."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_media_preview as adapter
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_media_preview.py"


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.done = False

    async def read(self, _limit: int) -> bytes:
        if self.done:
            return b""
        self.done = True
        return self.body


class _StalledContent:
    def __init__(self) -> None:
        self.entered = False
        self.cancelled = False

    async def read(self, _limit: int) -> bytes:
        self.entered = True
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True
        raise AssertionError("stalled content unexpectedly resumed")


class _Headers:
    def __init__(self, origins: list[str], ranges: list[str] | None = None) -> None:
        self.origins = origins
        self.ranges = ranges or []

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        if name == "Origin":
            return self.origins
        if name == "Range":
            return self.ranges
        return default


class _Response:
    def __init__(
        self,
        *,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


class _StreamResponse(_Response):
    def __init__(self, *, status: int, headers: dict[str, str]) -> None:
        super().__init__(status=status, headers=headers)
        self.prepared = False
        self.eof = False

    async def prepare(self, _request: object) -> None:
        self.prepared = True

    async def write(self, body: bytes) -> None:
        self.body += body

    async def write_eof(self) -> None:
        self.eof = True


def _web() -> SimpleNamespace:
    return SimpleNamespace(Response=_Response, StreamResponse=_StreamResponse)


def _register(routes: _Routes) -> Callable[..., Any]:
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = _web()
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False):
            assert adapter.ensure_media_preview_route_registered()
            assert adapter.ensure_media_preview_route_registered()
    assert len(routes) == 1
    return cast(Callable[..., Any], routes[0].handler)


def _body() -> bytes:
    return json.dumps(
        {
            "schema": adapter.MEDIA_PREVIEW_REQUEST_SCHEMA,
            "workspace_handle": "pw_" + "a" * 40,
            "expected_workspace_revision": 1,
            "expected_workspace_fingerprint": "sha256:" + "b" * 64,
            "output_handle": "out_" + "c" * 40,
        }
    ).encode("utf-8")


def _request(
    *,
    body: bytes | None = None,
    origins: list[str] | None = None,
    ranges: list[str] | None = None,
    content_type: str = "application/json",
    query: str = "",
) -> SimpleNamespace:
    payload = _body() if body is None else body
    return SimpleNamespace(
        content_type=content_type,
        content_length=len(payload),
        content=_Content(payload),
        headers=_Headers(
            ["http://127.0.0.1:8188"] if origins is None else origins,
            ranges,
        ),
        query_string=query,
        transport=ListenerTransport(),
    )


def test_optional_imports_are_lazy_and_foreign_collision_is_not_claimed() -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    top_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_imports.add(node.module.split(".", 1)[0])
    assert {"aiohttp", "server"}.isdisjoint(top_imports)

    def foreign(_request: object) -> None:
        return None

    routes = _Routes(
        [SimpleNamespace(method="POST", path=adapter.MEDIA_PREVIEW_ROUTE, handler=foreign)]
    )
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = _web()
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False):
            assert not adapter.ensure_media_preview_route_registered()
    assert routes[0].handler is foreign


@pytest.mark.parametrize(
    ("request_value", "status"),
    (
        (_request(origins=[]), 403),
        (_request(origins=["http://foreign.invalid"]), 403),
        (_request(ranges=["bytes=0-1"]), 400),
        (_request(query="codec=h264"), 400),
        (_request(content_type="text/plain"), 415),
    ),
)
def test_prelookup_rejections_are_empty_and_do_not_create_worker(
    request_value: SimpleNamespace,
    status: int,
) -> None:
    handler = _register(_Routes())
    with (
        patch.object(adapter, "_EXECUTOR", None),
        patch.object(adapter, "admit_media_preview_source") as admit,
    ):
        response = asyncio.run(handler(request_value))
        assert (response.status, response.body) == (status, b"")
        assert response.headers == {"Cache-Control": "no-store", "Content-Length": "0"}
        assert adapter._EXECUTOR is None
        admit.assert_not_called()


def test_stalled_request_body_stops_at_work_deadline_before_lookup_or_worker() -> None:
    handler = _register(_Routes())
    request = _request()
    stalled = _StalledContent()
    request.content = stalled

    async def exercise() -> _StreamResponse:
        return cast(
            _StreamResponse,
            await asyncio.wait_for(handler(request), timeout=0.25),
        )

    with (
        patch.object(adapter, "_WORK_DEADLINE_SECONDS", 0.02),
        patch.object(adapter, "_REQUEST_DEADLINE_SECONDS", 0.20),
        patch.object(adapter, "_EXECUTOR", None),
        patch.object(adapter, "admit_media_preview_source") as admit,
    ):
        response = asyncio.run(exercise())

    assert (response.status, response.body) == (504, b"")
    assert response.prepared and response.eof
    assert stalled.entered and stalled.cancelled
    assert adapter._EXECUTOR is None
    admit.assert_not_called()


def test_transport_close_while_body_read_is_pending_stops_before_lookup_or_worker() -> None:
    handler = _register(_Routes())
    request = _request()
    stalled = _StalledContent()
    request.content = stalled
    closing = False
    request.transport = SimpleNamespace(
        get_extra_info=ListenerTransport().get_extra_info, is_closing=lambda: closing
    )

    async def exercise() -> object:
        nonlocal closing
        task = asyncio.create_task(handler(request))
        while not stalled.entered:
            await asyncio.sleep(0)
        closing = True
        return await asyncio.wait_for(task, timeout=0.25)

    with (
        patch.object(adapter, "_EXECUTOR", None),
        patch.object(adapter, "admit_media_preview_source") as admit,
    ):
        response = asyncio.run(exercise())
    # M25-42: a client that has gone still receives the owned bodiless response object.
    assert isinstance(response, _StreamResponse)
    assert (response.status, response.body, response.prepared) == (499, b"", False)
    assert stalled.cancelled
    assert adapter._EXECUTOR is None
    admit.assert_not_called()


def test_source_admission_crossing_work_deadline_does_not_submit_worker() -> None:
    handler = _register(_Routes())
    submissions = 0
    source = SimpleNamespace(render=lambda **_kwargs: b"must-not-render")

    def delayed_admission(**_kwargs: object) -> object:
        time.sleep(0.03)
        return source

    class _RecordingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> Future[Any]:
            nonlocal submissions
            submissions += 1
            future: Future[Any] = Future()
            future.set_result(adapter.OwnedPreviewBody(b"unexpected"))
            return future

    with (
        patch.object(adapter, "_WORK_DEADLINE_SECONDS", 0.01),
        patch.object(adapter, "_REQUEST_DEADLINE_SECONDS", 0.20),
        patch.object(adapter, "_EXECUTION_CLAIM", threading.Lock()),
        patch.object(adapter, "_executor", return_value=_RecordingExecutor()),
        patch.object(adapter, "admit_media_preview_source", side_effect=delayed_admission),
        patch.object(adapter, "media_preview_source_is_current", return_value=True),
    ):
        response = asyncio.run(handler(_request()))

    assert (response.status, response.body) == (504, b"")
    assert response.prepared and response.eof
    assert submissions == 0


def test_success_writes_one_exact_bounded_body_before_releasing_claim() -> None:
    handler = _register(_Routes())
    payload = b"bounded-derivative-mp4"
    source = SimpleNamespace(render=lambda **_kwargs: payload)

    class _ImmediateExecutor:
        def submit(
            self,
            function: Callable[..., Any],
            *args: object,
            **kwargs: object,
        ) -> Future[Any]:
            future: Future[Any] = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as exc:  # pragma: no cover - assertion aid
                future.set_exception(exc)
            return future

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", threading.Lock()),
        patch.object(adapter, "_executor", return_value=_ImmediateExecutor()),
        patch.object(adapter, "admit_media_preview_source", return_value=source),
        patch.object(adapter, "media_preview_source_is_current", return_value=True),
    ):
        response = asyncio.run(handler(_request()))

    assert response.status == 200
    assert response.body == payload
    assert response.prepared and response.eof
    assert response.headers == {
        "Content-Type": "video/mp4",
        "Content-Length": str(len(payload)),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": 'inline; filename="h3-preview.mp4"',
    }


def test_busy_request_never_submits_or_queues_work() -> None:
    handler = _register(_Routes())
    claim = threading.Lock()
    claim.acquire()
    try:
        with (
            patch.object(adapter, "_EXECUTION_CLAIM", claim),
            patch.object(adapter, "_executor") as executor,
            patch.object(
                adapter,
                "admit_media_preview_source",
                return_value=SimpleNamespace(),
            ),
        ):
            response = asyncio.run(handler(_request()))
        assert (response.status, response.body) == (423, b"")
        executor.assert_not_called()
    finally:
        claim.release()


@pytest.mark.parametrize(
    ("cancelled", "error_code", "status"),
    (
        (True, None, 500),
        (False, "preview_deadline", 504),
        (False, "media_output_invalid", 422),
        (False, "cleanup_failed", 500),
        (False, None, 500),
    ),
)
def test_failed_or_cancelled_future_uses_closed_status_and_releases_claim(
    cancelled: bool,
    error_code: str | None,
    status: int,
) -> None:
    handler = _register(_Routes())
    claim = threading.Lock()

    class _TerminalExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> Future[Any]:
            future: Future[Any] = Future()
            if cancelled:
                future.cancel()
            else:
                failure = RuntimeError("private worker failure")
                if error_code is not None:
                    failure.code = error_code  # type: ignore[attr-defined]
                future.set_exception(failure)
            return future

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_TerminalExecutor()),
        patch.object(
            adapter,
            "admit_media_preview_source",
            return_value=SimpleNamespace(),
        ),
    ):
        response = asyncio.run(handler(_request()))

    assert (response.status, response.body) == (status, b"")
    assert claim.acquire(blocking=False)
    claim.release()


def test_empty_response_prepare_and_eof_share_the_request_deadline() -> None:
    class _Clock:
        def __init__(self) -> None:
            self.values = iter((0.0, 59.0))
            self.value = 59.0

        def __call__(self) -> float:
            self.value = next(self.values, self.value)
            return self.value

    clock = _Clock()
    request = _request(origins=[])
    aborted: list[bool] = []
    request.transport = SimpleNamespace(
        get_extra_info=ListenerTransport().get_extra_info, abort=lambda: aborted.append(True)
    )
    handler = _register(_Routes())

    async def stalled_prepare(_response: _StreamResponse, _request: object) -> None:
        await asyncio.Event().wait()

    async def exercise() -> object:
        with (
            patch.object(time, "monotonic", side_effect=clock),
            patch.object(_StreamResponse, "prepare", stalled_prepare),
        ):
            return await handler(request)

    async def timeout(awaitable: object, **_kwargs: object) -> object:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    with patch.object(asyncio, "wait_for", side_effect=timeout):
        response = asyncio.run(exercise())
    # M25-42: the aborted attempt is returned to the host rather than nothing.
    assert isinstance(response, _StreamResponse)
    assert (response.status, response.body, response.eof) == (403, b"", False)
    assert aborted == [True]


def test_submit_failure_and_late_source_validation_release_exactly_once() -> None:
    handler = _register(_Routes())
    claim = threading.Lock()

    class _RejectingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> Future[Any]:
            raise RuntimeError("private submit failure")

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_RejectingExecutor()),
        patch.object(
            adapter,
            "admit_media_preview_source",
            return_value=SimpleNamespace(),
        ),
    ):
        response = asyncio.run(handler(_request()))
    assert (response.status, response.body) == (500, b"")
    assert claim.acquire(blocking=False)
    claim.release()

    owned = adapter.OwnedPreviewBody(b"late-private-body")

    class _CompletedExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_result(owned)
            return future

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_CompletedExecutor()),
        patch.object(
            adapter,
            "admit_media_preview_source",
            return_value=SimpleNamespace(),
        ),
        patch.object(adapter, "media_preview_source_is_current", return_value=False),
    ):
        response = asyncio.run(handler(_request()))
    assert (response.status, response.body) == (409, b"")
    with pytest.raises(RuntimeError, match="unavailable"):
        owned.take()
    assert claim.acquire(blocking=False)
    claim.release()


def test_slow_response_holds_claim_and_post_take_failure_releases_it() -> None:
    handler = _register(_Routes())
    claim = threading.Lock()
    source = SimpleNamespace(render=lambda **_kwargs: b"bounded-private-body")

    class _ImmediateExecutor:
        def submit(
            self,
            function: Callable[..., Any],
            *args: object,
            **kwargs: object,
        ) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_result(function(*args, **kwargs))
            return future

    async def exercise_slow_write() -> tuple[Any, Any]:
        entered = asyncio.Event()
        release = asyncio.Event()
        original_write = _StreamResponse.write

        async def slow_write(response: _StreamResponse, body: bytes) -> None:
            entered.set()
            await release.wait()
            await original_write(response, body)

        with patch.object(_StreamResponse, "write", slow_write):
            first_task = asyncio.create_task(handler(_request()))
            await entered.wait()
            second = await handler(_request())
            release.set()
            first = await first_task
        return first, second

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_ImmediateExecutor()),
        patch.object(adapter, "admit_media_preview_source", return_value=source),
        patch.object(adapter, "media_preview_source_is_current", return_value=True),
    ):
        first, second = asyncio.run(exercise_slow_write())
    assert first.status == 200
    assert (second.status, second.body) == (423, b"")
    assert claim.acquire(blocking=False)
    claim.release()


@pytest.mark.parametrize("phase", ("prepare", "write", "write_eof"))
def test_success_response_failure_aborts_transport_and_releases_claim(phase: str) -> None:
    handler = _register(_Routes())
    claim = threading.Lock()
    source = SimpleNamespace(render=lambda **_kwargs: b"bounded-private-body")
    aborted: list[bool] = []
    request = _request()
    request.transport = SimpleNamespace(
        get_extra_info=ListenerTransport().get_extra_info,
        is_closing=lambda: False,
        abort=lambda: aborted.append(True),
    )

    class _ImmediateExecutor:
        def submit(
            self,
            function: Callable[..., Any],
            *args: object,
            **kwargs: object,
        ) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_result(function(*args, **kwargs))
            return future

    async def fail_phase(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("private response timeout")

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_ImmediateExecutor()),
        patch.object(adapter, "admit_media_preview_source", return_value=source),
        patch.object(adapter, "media_preview_source_is_current", return_value=True),
        patch.object(_StreamResponse, phase, fail_phase),
    ):
        response = asyncio.run(handler(request))
    # M25-42: the aborted success attempt is returned to the host rather than nothing.
    assert isinstance(response, _StreamResponse)
    assert (response.status, response.eof) == (200, False)
    assert aborted == [True]
    assert claim.acquire(blocking=False)
    claim.release()


def test_success_response_cancellation_aborts_transport_and_releases_claim() -> None:
    handler = _register(_Routes())
    claim = threading.Lock()
    source = SimpleNamespace(render=lambda **_kwargs: b"bounded-private-body")
    aborted: list[bool] = []
    request = _request()
    request.transport = SimpleNamespace(
        get_extra_info=ListenerTransport().get_extra_info,
        is_closing=lambda: False,
        abort=lambda: aborted.append(True),
    )

    class _ImmediateExecutor:
        def submit(
            self,
            function: Callable[..., Any],
            *args: object,
            **kwargs: object,
        ) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_result(function(*args, **kwargs))
            return future

    async def cancel_write(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    with (
        patch.object(adapter, "_EXECUTION_CLAIM", claim),
        patch.object(adapter, "_executor", return_value=_ImmediateExecutor()),
        patch.object(adapter, "admit_media_preview_source", return_value=source),
        patch.object(adapter, "media_preview_source_is_current", return_value=True),
        patch.object(_StreamResponse, "write", cancel_write),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(handler(request))
    assert aborted == [True]
    assert claim.acquire(blocking=False)
    claim.release()
