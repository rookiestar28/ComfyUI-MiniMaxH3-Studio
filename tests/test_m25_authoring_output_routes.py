"""Authoring output HTTP behavior with real jobs/store and a synthetic renderer."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport
from deployment_request_doubles import LOOPBACK_ORIGIN as OWNED_ORIGIN
from test_m25_authoring_output_service import completed
from test_m25_authoring_output_service import subject as subject
from test_m25_render_job_leases import image_bound as image_bound

from comfyui_h3_context.adapters.comfyui_route_seam import (
    OriginRule,
    origin_accepted,
)


def routes_module() -> Any:
    name = "comfyui_h3_context.adapters.comfyui_authoring_output"
    assert importlib.util.find_spec(name) is not None, "authoring output HTTP edge is missing"
    return importlib.import_module(name)


def test_supplied_host_api_prefix_clone_and_head_refusal() -> None:
    web = pytest.importorskip("aiohttp.web")
    utilities = pytest.importorskip("aiohttp.test_utils")

    async def run() -> None:
        def forbidden_registry() -> Any:
            pytest.fail("HEAD reached output authority")

        owner = routes_module().AuthoringOutputRoutes(forbidden_registry)
        owner._admit = lambda: pytest.fail("HEAD consumed transport admission")
        routes = web.RouteTableDef()
        assert owner.register(web, routes)
        api_routes = web.RouteTableDef()
        # The supplied PromptServer clones route kwargs at decorator invocation, not creation.
        for route in routes:
            api_routes.route(route.method, "/api" + route.path)(route.handler, **route.kwargs)
        app = web.Application()
        app.add_routes(api_routes)
        app.add_routes(routes)
        client = utilities.TestClient(utilities.TestServer(app))
        await client.start_server()
        try:
            for prefix in ("", "/api"):
                for path in (
                    "render/arj_" + "a" * 22,
                    "output/aro_" + "a" * 22 + "/preview",
                    "output/aro_" + "a" * 22 + "/download",
                ):
                    response = await client.head(prefix + "/h3-context/v1/authoring/" + path)
                    assert response.status == 400
                    assert await response.read() == b""
                    assert response.headers["Cache-Control"] == "private, no-store"
            assert owner._executor is None
        finally:
            await client.close()
            owner.close()

    asyncio.run(run())


class Headers:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = pairs

    def getall(self, key: str, default: list[str]) -> list[str]:
        return [value for name, value in self.pairs if name == key] or default


def test_same_origin_get_is_a_distinct_metadata_guard() -> None:
    rule = getattr(OriginRule, "SAME_ORIGIN_GET", None)
    assert rule is not None, "output GET needs its own fetch-metadata policy"
    for origins in ([], [OWNED_ORIGIN], [OWNED_ORIGIN, OWNED_ORIGIN], ["null"]):
        for sites in (
            [],
            ["same-origin"],
            ["same-site"],
            ["cross-site"],
            ["none"],
            ["same-origin", "same-origin"],
        ):
            headers = Headers(
                [("Host", LOOPBACK_HOST)]
                + [("Origin", value) for value in origins]
                + [("Sec-Fetch-Site", value) for value in sites]
            )
            request = SimpleNamespace(headers=headers, transport=ListenerTransport())
            assert origin_accepted(request, rule) == (
                origins in ([], [OWNED_ORIGIN]) and sites in ([], ["same-origin"])
            )
            # Existing route policies retain their accepted behavior.
            assert origin_accepted(request, OriginRule.EXACT) == (origins == [OWNED_ORIGIN])
            assert origin_accepted(request, OriginRule.EXACT_OR_ABSENT) == (
                origins in ([], [OWNED_ORIGIN])
            )


def test_output_http_edge_exists() -> None:
    assert callable(routes_module().AuthoringOutputRoutes)


def test_real_http_status_range_cancel_and_closed_refusals(subject: Any) -> None:
    web = pytest.importorskip("aiohttp.web")
    test_utils = pytest.importorskip("aiohttp.test_utils")

    module = routes_module()
    registry, request, workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)

    async def scenario() -> None:
        edge = module.AuthoringOutputRoutes(lambda: registry)
        app = web.Application()
        table = web.RouteTableDef()
        assert edge.register(web, table)
        assert edge.register(web, table)
        assert len(table) == 6
        app.add_routes(table)
        client = test_utils.TestClient(test_utils.TestServer(app))
        await client.start_server()
        # M23-57: a same-origin request names this test server, never a fixed deployment.
        own = f"http://{client.host}:{client.port}"
        job = module.AUTHORING_RENDER_ROUTE + "/" + status["job_handle"]
        output = module.AUTHORING_OUTPUT_ROUTE + "/" + status["output_handle"]
        query = {"workspace_handle": request.workspace_handle}
        try:
            # M25-16: the capability read is same-origin GET, bodiless, and reports the live
            # registry; a query string, a foreign origin or an implicit HEAD are refused.
            response = await client.get(module.AUTHORING_OUTPUT_CAPABILITY_ROUTE)
            assert response.status == 200
            capability = await response.json()
            assert capability["schema"] == "h3.authoring.output_capability.v1"
            assert capability["supported"] is True
            assert response.headers["Cache-Control"] == "private, no-store"
            response = await client.get(module.AUTHORING_OUTPUT_CAPABILITY_ROUTE, params={"x": "1"})
            assert response.status == 400
            response = await client.get(
                module.AUTHORING_OUTPUT_CAPABILITY_ROUTE,
                headers={"Origin": "http://foreign.invalid"},
            )
            assert response.status == 403
            response = await client.head(module.AUTHORING_OUTPUT_CAPABILITY_ROUTE)
            assert response.status == 400
            # A GET that carries a body is not the bodiless read either; the guard refuses
            # it before any registry lookup rather than silently ignoring the payload.
            response = await client.get(
                module.AUTHORING_OUTPUT_CAPABILITY_ROUTE,
                data=b'{"supported": true}',
                headers={"Content-Type": "application/json"},
            )
            assert response.status == 400
            response = await client.get(job, params=query)
            assert response.status == 200
            assert await response.json() == status
            response = await client.post(
                module.AUTHORING_RENDER_ROUTE,
                json=request.to_wire(),
                headers={"Origin": own},
            )
            assert response.status == 200
            assert (await response.json())["job_handle"] == status["job_handle"]
            for range_header, expected in (
                (None, 200),
                ("bytes=0-3", 206),
                ("bytes=-0", 416),
                ("bytes=3-2", 400),
            ):
                headers: Any = {} if range_header is None else {"Range": range_header}
                response = await client.get(output + "/download", params=query, headers=headers)
                assert response.status == expected
                body = await response.read()
                assert response.headers["Cache-Control"] == "private, no-store"
                if expected in (200, 206, 416):
                    assert len(body) == int(response.headers["Content-Length"])
                    assert response.headers["Content-Type"] == "video/mp4"
                    if expected == 206:
                        assert len(body) == 4
                    if expected == 416:
                        assert not body
                # Receiving the declared length can precede the server's final EOF/finally.
                for _attempt in range(100):
                    if registry.active_responses == 0:
                        break
                    await asyncio.sleep(0.01)
                assert registry.active_responses == 0
            for headers in (
                {"Origin": "http://foreign.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
                [("Origin", own), ("Origin", own)],
            ):
                response = await client.get(job, params=query, headers=headers)
                assert response.status == 403
            for params in (
                {},
                {**query, "path": "private"},
                [("workspace_handle", request.workspace_handle)] * 2,
            ):
                response = await client.get(job, params=params)
                assert response.status == 400
            response = await client.post(
                job + "/cancel",
                json={"schema": "h3.authoring.output_cancel.v1", **query},
                headers={"Origin": own},
            )
            assert response.status == 200
            assert await response.json() == status
            response = await client.get(output + "/preview", params=query)
            assert response.status == 503
            assert (await response.json())["code"] == "preview_unavailable"
            response = await client.head(output + "/download", params=query)
            assert response.status == 400
            assert await response.read() == b""
            workspace.live = False
            response = await client.get(output + "/download", params=query)
            assert response.status == 404
            assert set(await response.json()) == {"schema", "code"}
        finally:
            await client.close()
            edge.close()

    asyncio.run(scenario())


def test_cancelled_http_tasks_retain_workers_and_do_not_cancel_jobs(subject: Any) -> None:
    web = pytest.importorskip("aiohttp.web")
    test_utils = pytest.importorskip("aiohttp.test_utils")
    registry, request, _workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)
    original_status = registry.status
    proceed = threading.Event()
    entered = [0]
    lock = threading.Lock()

    def slow_status(handle: str, workspace: str) -> Any:
        with lock:
            entered[0] += 1
        assert proceed.wait(5)
        return original_status(handle, workspace)

    registry.status = slow_status

    async def scenario() -> None:
        edge = routes_module().AuthoringOutputRoutes(lambda: registry)
        path = (
            routes_module().AUTHORING_RENDER_ROUTE
            + "/"
            + status["job_handle"]
            + "?workspace_handle="
            + request.workspace_handle
        )

        def incoming() -> Any:
            # M23-57: admission reads the Host and the accepting socket, which a bare mocked
            # request has neither of (its transport answers every lookup with a Mock).
            return test_utils.make_mocked_request(
                "GET",
                path,
                headers={"Host": LOOPBACK_HOST},
                match_info={"handle": status["job_handle"]},
                transport=ListenerTransport(),
            )

        tasks = [asyncio.create_task(edge._handle("status", incoming(), web)) for _ in range(4)]
        try:
            for _attempt in range(100):
                if entered[0] == 4:
                    break
                await asyncio.sleep(0.01)
            assert entered[0] == 4
            for task in tasks:
                task.cancel()
            assert all(
                isinstance(value, asyncio.CancelledError)
                for value in await asyncio.gather(*tasks, return_exceptions=True)
            )
            refused = await edge._handle("status", incoming(), web)
            assert refused.status == 429
            assert original_status(status["job_handle"], request.workspace_handle) == status
            proceed.set()
            for _attempt in range(100):
                response = await edge._handle("status", incoming(), web)
                if response.status == 200:
                    break
                assert response.status == 429
                await asyncio.sleep(0.01)
            assert response.status == 200
        finally:
            proceed.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            edge.close()
            registry.status = original_status

    asyncio.run(scenario())


def test_abandoned_download_worker_closes_its_late_verified_lease(subject: Any) -> None:
    web = pytest.importorskip("aiohttp.web")
    test_utils = pytest.importorskip("aiohttp.test_utils")
    registry, request, _workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)
    original_open = registry.open_download
    entered = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()

    def slow_open(handle: str, workspace: str, *, range_header: object = None) -> Any:
        entered.set()
        assert proceed.wait(5)
        try:
            return original_open(handle, workspace, range_header=range_header)
        finally:
            finished.set()

    registry.open_download = slow_open

    async def scenario() -> None:
        edge = routes_module().AuthoringOutputRoutes(lambda: registry)
        path = (
            routes_module().AUTHORING_OUTPUT_ROUTE
            + "/"
            + status["output_handle"]
            + "/download?workspace_handle="
            + request.workspace_handle
        )
        incoming = test_utils.make_mocked_request(
            "GET",
            path,
            headers={"Host": LOOPBACK_HOST},
            match_info={"handle": status["output_handle"]},
            transport=ListenerTransport(),
        )
        task = asyncio.create_task(edge._handle("download", incoming, web))
        try:
            for _attempt in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            proceed.set()
            for _attempt in range(100):
                if finished.is_set() and registry.active_responses == 0:
                    break
                await asyncio.sleep(0.01)
            assert finished.is_set() and registry.active_responses == 0
            with original_open(status["output_handle"], request.workspace_handle) as lease:
                assert b"".join(lease.chunks())
        finally:
            proceed.set()
            await asyncio.gather(task, return_exceptions=True)
            edge.close()
            registry.open_download = original_open

    asyncio.run(scenario())


def test_foreign_or_partial_route_ownership_is_not_overwritten() -> None:
    web = pytest.importorskip("aiohttp.web")
    module = routes_module()
    edge = module.AuthoringOutputRoutes(lambda: None)
    table = web.RouteTableDef()

    async def foreign(_request: Any) -> Any:
        return web.Response(status=418)

    table.get(module.AUTHORING_OUTPUT_ROUTE + "/{handle}/download")(foreign)
    try:
        assert not edge.register(web, table)
        assert len(table) == 1
        assert table[0].handler is foreign
    finally:
        edge.close()


def test_actual_disconnect_stops_pending_preview_without_cancelling_render(subject: Any) -> None:
    web = pytest.importorskip("aiohttp.web")
    test_utils = pytest.importorskip("aiohttp.test_utils")
    from aiohttp.test_utils import TestServer

    from comfyui_h3_context.core.authoring_output_protocol import OutputProtocolError

    registry, request, *_ = subject
    status = completed(registry, request)
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()

    class PendingPreview:
        def render(self, _body: bytes, _parent: Any, check: Any) -> Any:
            entered.set()
            while not release.wait(0.01):
                try:
                    check()
                except OutputProtocolError:
                    cancelled.set()
                    raise
            raise OutputProtocolError("preview_unavailable")

        def close(self) -> None:
            release.set()

    registry._preview_backend = PendingPreview()

    # The isolated hook omits optional aiohttp types; the qualification venv includes them.
    class HostDefaultServer(TestServer):  # type: ignore[misc, unused-ignore]
        async def _make_runner(self, **kwargs: Any) -> Any:
            # TestServer forces cancellation on; ComfyUI's AppRunner leaves it off.
            kwargs["handler_cancellation"] = False
            return await super()._make_runner(**kwargs)

    async def scenario() -> None:
        edge = routes_module().AuthoringOutputRoutes(lambda: registry)
        table = web.RouteTableDef()
        edge.register(web, table)
        app = web.Application()
        app.add_routes(table)
        async with test_utils.TestClient(HostDefaultServer(app)) as client:
            path = (
                routes_module().AUTHORING_OUTPUT_ROUTE
                + "/"
                + status["output_handle"]
                + "/preview?workspace_handle="
                + request.workspace_handle
            )
            task = asyncio.create_task(client.get(path))
            try:
                deadline = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                deadline = time.monotonic() + 2
                while not cancelled.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert cancelled.is_set(), "disconnected client left preview work running"
                assert registry.status(status["job_handle"], request.workspace_handle) == status
                assert registry.active_responses == 0
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                edge.close()

    asyncio.run(scenario())
