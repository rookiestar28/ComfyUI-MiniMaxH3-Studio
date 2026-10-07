"""M25-42: hand-written owned routes answer a client that has gone with a response object.

Every middleware in a ComfyUI host receives a handler's return value. A supplied host carries
foreign packs whose middleware reads it (LoRA-Manager's CSP relaxation calls
`response.headers.get`), and aiohttp itself logs a missing return as a server error. These cases
run each route through a real aiohttp application with the host's handler-cancellation default and
such a middleware.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from test_m25_authoring_output_service import completed
from test_m25_authoring_output_service import subject as subject
from test_m25_media_derivative_contract import create_wire
from test_m25_media_source_leases import Claim
from test_m25_render_job_leases import image_bound as image_bound

import comfyui_h3_context.adapters.comfyui_authoring_media_leases as lease_module
import comfyui_h3_context.adapters.comfyui_authoring_media_preview as authoring_preview_module
import comfyui_h3_context.adapters.comfyui_media_preview as production_preview_module
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.core.authoring_media import VerifiedDerivative
from comfyui_h3_context.core.authoring_preview_protocol import AUTHORING_PREVIEW_REQUEST_SCHEMA
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

web = pytest.importorskip("aiohttp.web")
test_utils = pytest.importorskip("aiohttp.test_utils")

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = ROOT / "comfyui_h3_context" / "adapters"
FP = "sha256:" + "a" * 64


# The pinned mypy hook has no aiohttp, so the module is `Any` there: bind the base to a name first.
_TestServer: Any = test_utils.TestServer


class HostDefaultServer(_TestServer):  # type: ignore[misc, unused-ignore]
    async def _make_runner(self, **kwargs: Any) -> Any:
        # CRITICAL: aiohttp's TestServer enables handler cancellation, which cancels the handler on
        # disconnect and never reaches its client-gone branch; ComfyUI's AppRunner leaves it off.
        kwargs["handler_cancellation"] = False
        return await super()._make_runner(**kwargs)


@dataclass
class Edge:
    returned: list[object] = field(default_factory=list)
    raised: list[BaseException] = field(default_factory=list)
    settled: asyncio.Event = field(default_factory=asyncio.Event)


def host_application(edge: Edge) -> Any:
    async def reads_response(request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        # The shape of a real foreign pack middleware: it reads the response it is handed.
        try:
            response = await handler(request)
        except BaseException as error:
            edge.raised.append(error)
            edge.settled.set()
            raise
        edge.returned.append(response)
        edge.settled.set()
        response.headers.get("Content-Security-Policy")
        return response

    return web.Application(middlewares=[web.middleware(reads_response)])


def handler_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("aiohttp") and record.levelno >= logging.ERROR
    ]


async def wait_until(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            pytest.fail("condition was not reached")
        await asyncio.sleep(0.01)


async def send_then_close(
    server: Any, request: bytes, ready: Callable[[], Awaitable[None]]
) -> None:
    _reader, writer = await asyncio.open_connection(server.host, server.port)
    writer.write(request)
    await writer.drain()
    await ready()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


def post(server: Any, path: str, body: bytes, *, declared: int | None = None) -> bytes:
    # M23-57: a same-origin request names the listener it is sent to, in both Host and Origin.
    authority = f"{server.host}:{server.port}"
    length = len(body) if declared is None else declared
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {authority}",
        f"Origin: http://{authority}",
        "Content-Type: application/json",
        f"Content-Length: {length}",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii") + body


def assert_response_reached_host(edge: Edge, caplog: pytest.LogCaptureFixture) -> Any:
    assert edge.raised == []
    assert len(edge.returned) == 1
    response = edge.returned[0]
    assert isinstance(response, web.StreamResponse), response
    assert handler_errors(caplog) == []
    return response


def test_lease_route_answers_a_client_gone_during_generation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started, finished = threading.Event(), threading.Event()

    class BlockingClaim(Claim):
        def generate(
            self, deadline: float, cancellation: Any
        ) -> tuple[bytearray, VerifiedDerivative]:
            started.set()
            while not cancellation.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            finished.set()
            return super().generate(deadline, cancellation)

    authority = MediaLeaseAuthority(lambda _: BlockingClaim(), start_reaper=False)
    service = lease_module.MediaLeaseRouteService(authority)
    routes = web.RouteTableDef()
    monkeypatch.setitem(sys.modules, "server", host_prompt_server_module(routes))
    monkeypatch.setattr(lease_module, "_SERVICE", service)
    assert lease_module.ensure_authoring_media_lease_routes_registered()
    edge = Edge()
    app = host_application(edge)
    app.add_routes(routes)

    async def run() -> None:
        async with HostDefaultServer(app) as server:

            async def generating() -> None:
                await wait_until(started.is_set)

            body = json.dumps(create_wire()).encode()
            await send_then_close(server, post(server, lease_module.LEASE_ROUTE, body), generating)
            await asyncio.wait_for(edge.settled.wait(), 5)
            await asyncio.sleep(0.2)
            response = assert_response_reached_host(edge, caplog)
            assert response.status == 499
            assert json.loads(response.body)["reason"] == "cancelled"
            await wait_until(lambda: finished.is_set() and not service._claim.locked())
            assert authority.resources()["cacheBytes"] == 0
            assert authority.resources()["leases"] == 0

    try:
        asyncio.run(run())
    finally:
        service.close()


def test_authoring_preview_route_answers_a_client_gone_during_conversion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started, observed_cancel = threading.Event(), threading.Event()

    class BlockingClaim:
        def render(self, _adapter: object, *, deadline: float, cancellation: Any) -> Any:
            started.set()
            while not cancellation.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            if cancellation.is_cancelled():
                observed_cancel.set()
            return bytearray(b"late-private-body"), "absent"

    media_adapter = object.__new__(QualifiedAVMediaAdapter)
    claim_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=1)
    routes = web.RouteTableDef()
    monkeypatch.setitem(sys.modules, "server", host_prompt_server_module(routes))
    monkeypatch.setattr(authoring_preview_module, "_ROUTE_REGISTERED", False)
    assert authoring_preview_module.ensure_authoring_media_preview_route_registered()
    edge = Edge()
    app = host_application(edge)
    app.add_routes(routes)
    body = json.dumps(
        {
            "schema": AUTHORING_PREVIEW_REQUEST_SCHEMA,
            "requestId": "request-1",
            "workspaceHandle": "authoring-1",
            "referenceRevision": 1,
            "timelineRevision": 1,
            "timelineContentFingerprint": FP,
            "clipId": "clip-1",
        }
    ).encode()

    async def run() -> None:
        async with HostDefaultServer(app) as server:

            async def converting() -> None:
                await wait_until(started.is_set)

            await send_then_close(
                server,
                post(server, authoring_preview_module.AUTHORING_MEDIA_PREVIEW_ROUTE, body),
                converting,
            )
            await asyncio.wait_for(edge.settled.wait(), 5)
            await asyncio.sleep(0.2)
            response = assert_response_reached_host(edge, caplog)
            assert response.status == 499
            assert json.loads(response.body)["reason"] == "cancelled"
            await wait_until(lambda: observed_cancel.is_set() and not claim_lock.locked())

    authoring_preview_module.publish_authoring_media_preview_adapter(media_adapter)
    try:
        with (
            patch.object(authoring_preview_module, "_EXECUTION_CLAIM", claim_lock),
            patch.object(authoring_preview_module, "_executor", return_value=executor),
            patch.object(
                authoring_preview_module,
                "admit_authoring_media_preview",
                return_value=BlockingClaim(),
            ),
        ):
            asyncio.run(run())
    finally:
        authoring_preview_module.clear_authoring_media_preview_adapter(media_adapter)
        executor.shutdown(wait=True)


def production_preview_body() -> bytes:
    return json.dumps(
        {
            "schema": production_preview_module.MEDIA_PREVIEW_REQUEST_SCHEMA,
            "workspace_handle": "pw_" + "a" * 40,
            "expected_workspace_revision": 1,
            "expected_workspace_fingerprint": "sha256:" + "b" * 64,
            "output_handle": "out_" + "c" * 40,
        }
    ).encode("utf-8")


def production_preview_application(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Edge, Any]:
    routes = web.RouteTableDef()
    monkeypatch.setitem(sys.modules, "server", host_prompt_server_module(routes))
    monkeypatch.setattr(production_preview_module, "_ROUTE_REGISTERED", False)
    assert production_preview_module.ensure_media_preview_route_registered()
    edge = Edge()
    app = host_application(edge)
    app.add_routes(routes)
    return edge, app


def test_production_preview_route_answers_a_client_gone_before_its_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    edge, app = production_preview_application(monkeypatch)

    async def run() -> None:
        async with HostDefaultServer(app) as server:

            async def headers_only() -> None:
                await asyncio.sleep(0.3)

            request = post(server, production_preview_module.MEDIA_PREVIEW_ROUTE, b"", declared=64)
            await send_then_close(server, request, headers_only)
            await asyncio.wait_for(edge.settled.wait(), 5)
            await asyncio.sleep(0.2)
            response = assert_response_reached_host(edge, caplog)
            # The owned bodiless surface: 499 when the admission read observes the closed
            # transport, 500 when the reader reports the reset first. Neither reaches the client.
            assert response.status in {499, 500}
            assert response.headers["Content-Length"] == "0"

    with patch.object(production_preview_module, "admit_media_preview_source") as admit:
        asyncio.run(run())
    admit.assert_not_called()


def test_production_preview_route_answers_a_client_gone_during_rendering(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    edge, app = production_preview_application(monkeypatch)
    started, observed_cancel = threading.Event(), threading.Event()

    class BlockingSource:
        def render(self, *, deadline: float, cancellation: Any) -> bytes:
            started.set()
            while not cancellation.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            if cancellation.is_cancelled():
                observed_cancel.set()
            return b"late-private-body"

    claim_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=1)

    async def run() -> None:
        async with HostDefaultServer(app) as server:

            async def rendering() -> None:
                await wait_until(started.is_set)

            request = post(
                server, production_preview_module.MEDIA_PREVIEW_ROUTE, production_preview_body()
            )
            await send_then_close(server, request, rendering)
            await asyncio.wait_for(edge.settled.wait(), 5)
            await asyncio.sleep(0.2)
            response = assert_response_reached_host(edge, caplog)
            assert response.status == 499
            assert response.headers["Content-Length"] == "0"
            await wait_until(lambda: observed_cancel.is_set() and not claim_lock.locked())

    try:
        with (
            patch.object(production_preview_module, "_EXECUTION_CLAIM", claim_lock),
            patch.object(production_preview_module, "_executor", return_value=executor),
            patch.object(
                production_preview_module,
                "admit_media_preview_source",
                return_value=BlockingSource(),
            ),
            patch.object(
                production_preview_module, "media_preview_source_is_current", return_value=True
            ),
        ):
            asyncio.run(run())
    finally:
        executor.shutdown(wait=True)


def test_output_download_interrupted_after_headers_returns_its_aborted_response(
    subject: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    import comfyui_h3_context.adapters.comfyui_authoring_output as output_module

    registry, request, *_ = subject
    status = completed(registry, request)
    original_open = registry.open_download
    first_sent, resume = threading.Event(), threading.Event()

    class HeldLease:
        def __init__(self, lease: Any) -> None:
            self._lease = lease
            self.selection = lease.selection
            self.headers = lease.headers

        def chunks(self) -> Iterator[Any]:
            # Send part of the body only: a client that received its whole declared length returns
            # the connection to its pool on close instead of disconnecting.
            iterator = self._lease.chunks()
            first = bytes(next(iterator))
            assert len(first) > 1
            yield first[: len(first) // 2]
            first_sent.set()
            assert resume.wait(5)
            yield first[len(first) // 2 :]
            yield from iterator

        def close(self) -> None:
            self._lease.close()

    def held_open(handle: str, workspace: str, *, range_header: object = None) -> Any:
        return HeldLease(original_open(handle, workspace, range_header=range_header))

    registry.open_download = held_open
    edge = Edge()

    async def run() -> None:
        routes_owner = output_module.AuthoringOutputRoutes(lambda: registry)
        table = web.RouteTableDef()
        assert routes_owner.register(web, table)
        app = host_application(edge)
        app.add_routes(table)
        try:
            async with test_utils.TestClient(HostDefaultServer(app)) as client:
                path = (
                    output_module.AUTHORING_OUTPUT_ROUTE
                    + "/"
                    + status["output_handle"]
                    + "/download?workspace_handle="
                    + request.workspace_handle
                )
                response = await client.get(path)
                assert response.status == 200
                await wait_until(first_sent.is_set)
                response.close()
                await asyncio.sleep(0.2)
                resume.set()
                await asyncio.wait_for(edge.settled.wait(), 5)
                await asyncio.sleep(0.2)
                returned = assert_response_reached_host(edge, caplog)
                assert returned.prepared
                await wait_until(lambda: registry.active_responses == 0)
        finally:
            resume.set()
            routes_owner.close()

    try:
        asyncio.run(run())
    finally:
        registry.open_download = original_open


def _handler_functions() -> Iterator[tuple[str, ast.AST]]:
    targets = {
        "comfyui_authoring_media_leases.py": {"MediaLeaseRouteService._handle"},
        "comfyui_authoring_media_preview.py": {
            "ensure_authoring_media_preview_route_registered.authoring_media_preview"
        },
        "comfyui_media_preview.py": {
            "_emit_empty",
            "ensure_media_preview_route_registered.media_preview",
        },
        "comfyui_authoring_output.py": {
            "AuthoringOutputRoutes._handle",
            "AuthoringOutputRoutes._stream",
        },
    }
    for module, names in targets.items():
        tree = ast.parse((ADAPTERS / module).read_text(encoding="utf-8"))
        found: dict[str, ast.AST] = {}
        pending: list[tuple[ast.AST, str]] = [(tree, "")]
        while pending:
            node, prefix = pending.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    if name in names:
                        found[name] = child
                    pending.append((child, name + "."))
                else:
                    pending.append((child, prefix))
        assert set(found) == names, (module, names - set(found))
        for name, function in found.items():
            yield f"{module}:{name}", function


def _own_statements(function: ast.AST) -> Iterator[ast.AST]:
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


@pytest.mark.parametrize("name, function", list(_handler_functions()))
def test_hand_written_handlers_never_return_nothing_or_raise_a_disconnect(
    name: str, function: ast.AST
) -> None:
    for node in _own_statements(function):
        if isinstance(node, ast.Return):
            assert node.value is not None, f"{name}:{node.lineno} returns nothing"
            assert not (isinstance(node.value, ast.Constant) and node.value.value is None), (
                f"{name}:{node.lineno} returns None"
            )
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            called = node.exc.func
            assert not (
                isinstance(called, ast.Name)
                and called.id in {"ConnectionResetError", "ConnectionError"}
            ), f"{name}:{node.lineno} raises a disconnect into the host"
