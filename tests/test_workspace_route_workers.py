"""Blocking owned dispatch must leave loop progress and cancellation ownership intact."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from comfyui_h3_context.adapters import (
    comfyui_authoring_workspace as authoring,
)
from comfyui_h3_context.adapters import (
    comfyui_production_workspace as production,
)
from comfyui_h3_context.adapters import (
    comfyui_route_seam as seam,
)
from comfyui_h3_context.adapters import (
    comfyui_sequence_coordinator as coordinator,
)
from comfyui_h3_context.adapters import (
    comfyui_sidebar_workspace as sidebar,
)
from comfyui_h3_context.adapters import (
    production_planning_service as planning,
)
from comfyui_h3_context.adapters.route_workers import RouteWorker, RouteWorkerCapacityError
from scripts.hc_09_host_seam_test_double import host_prompt_server_module


def test_concurrent_route_mutations_preserve_actual_workspace_cas() -> None:
    from test_sidebar_workspace_adapter import _authority

    from comfyui_h3_context.core import SidebarWorkspaceError

    report, wiring, correlation = _authority()
    registry = sidebar.SidebarWorkspaceRegistry()
    initial = registry.publish(report, wiring, correlation)
    payload = json.dumps(
        {
            "schema": "h3.context.sidebar.action.v2",
            "workspace_id": initial.workspace_id,
            "expected_revision": initial.report_revision,
            "expected_report_fingerprint": initial.report_fingerprint,
            "action": "stage_prompt",
            "payload": {
                "reason": "Clarify motion",
                "prompt_text": initial.prompt_text.replace(
                    "A safe operator-owned prompt.",
                    "A safe operator-owned prompt, held in a slow pan.",
                ),
            },
        }
    ).encode()
    barrier = threading.Barrier(2)

    def dispatch(action: object) -> Any:
        barrier.wait(5)
        return registry.dispatch(action)

    async def scenario() -> None:
        with patch.object(sidebar, "dispatch_sidebar_action", side_effect=dispatch):
            results = await asyncio.gather(
                sidebar._act(payload, None), sidebar._act(payload, None), return_exceptions=True
            )
        completed = [result for result in results if isinstance(result, seam.RouteResult)]
        refused = [result for result in results if isinstance(result, SidebarWorkspaceError)]
        assert len(completed) == len(refused) == 1
        assert completed[0].status == 200
        assert refused[0].code == "stale_action"
        current = registry.get(initial.workspace_id)
        assert current.report_revision == initial.report_revision + 1
        assert current.to_wire() == completed[0].wire

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "module,decode,dispatch,result",
    [
        (sidebar, "decode_sidebar_action_json", "dispatch_sidebar_action", {"ready": True}),
        (
            production,
            "decode_production_action_json",
            "dispatch_production_action",
            SimpleNamespace(
                status=200, projection=SimpleNamespace(to_wire=lambda: {"ready": True})
            ),
        ),
        (
            authoring,
            "decode_authoring_action_json",
            "dispatch_authoring_action",
            SimpleNamespace(status=200, body={"ready": True}),
        ),
        (
            planning,
            "decode_production_planning_json",
            "dispatch_production_planning_action",
            SimpleNamespace(status=200, to_wire=lambda: {"ready": True}),
        ),
    ],
)
def test_each_route_yields_before_blocking_dispatch(
    module: ModuleType, decode: str, dispatch: str, result: Any
) -> None:
    entered, heartbeat, release = threading.Event(), threading.Event(), threading.Event()
    observation: list[bool] = []

    def blocking(_action: object) -> Any:
        entered.set()
        assert release.wait(5), "test barrier was not released"
        return result

    def observer() -> None:
        assert entered.wait(5)
        observation.append(heartbeat.wait(1))
        release.set()

    async def scenario() -> None:
        task = asyncio.create_task(module._act(b"{}", None))
        asyncio.get_running_loop().call_soon(heartbeat.set)
        response = await task
        assert response.status == 200 and response.wire == {"ready": True}

    watcher = threading.Thread(target=observer, daemon=True)
    watcher.start()
    try:
        with (
            patch.object(module, decode, return_value={"action": "read_workspace"}),
            patch.object(module, dispatch, side_effect=blocking),
        ):
            asyncio.run(scenario())
    finally:
        release.set()
        watcher.join(5)
    assert observation == [True], "dispatch held the event loop until the barrier was released"


@pytest.mark.parametrize("lane", ("planning", "retained_cleanup"))
def test_cancelled_waiter_keeps_capacity_until_actual_completion(lane: str) -> None:
    worker = RouteWorker(lane)
    release = threading.Event()
    lock = threading.Lock()
    admitted = 0
    commits: list[int] = []

    async def scenario() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocking() -> int:
            nonlocal admitted
            with lock:
                admitted += 1
                number = admitted
                if admitted == 2:
                    loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            commits.append(number)
            return number

        first = asyncio.create_task(worker.run(blocking))
        second = asyncio.create_task(worker.run(blocking))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(RouteWorkerCapacityError):
                await worker.run(lambda: 3)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(RouteWorkerCapacityError):
                await worker.run(lambda: 3)
            assert commits == []
        finally:
            release.set()
            await second
            worker._executor.shutdown(wait=True)
        assert sorted(commits) == [1, 2]
        assert admitted == 2

    asyncio.run(scenario())


def test_context_and_exception_are_preserved_and_capacity_is_released() -> None:
    worker = RouteWorker("production")
    marker: contextvars.ContextVar[str] = contextvars.ContextVar(
        "owned-route-marker", default="unset"
    )

    async def scenario() -> None:
        token = marker.set("request-context")
        try:
            assert await worker.run(marker.get) == "request-context"

            def failing() -> str:
                raise ValueError("controlled_failure")

            with pytest.raises(ValueError, match="controlled_failure"):
                await worker.run(failing)
            assert await worker.run(marker.get) == "request-context"
        finally:
            marker.reset(token)

    try:
        asyncio.run(scenario())
    finally:
        worker._executor.shutdown(wait=True)


def test_two_coordinator_media_jobs_leave_control_lane_available() -> None:
    release, lock = threading.Event(), threading.Lock()
    count = 0

    async def scenario() -> None:
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def dispatch(action: dict[str, Any]) -> Any:
            nonlocal count
            if action["action"] == "record_artifact":
                with lock:
                    count += 1
                    if count == 2:
                        loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
            return SimpleNamespace(status=204, response=None)

        with (
            patch.object(coordinator, "decode_coordinator_action_json", side_effect=json.loads),
            patch.object(coordinator, "dispatch_sequence_coordinator_action", side_effect=dispatch),
        ):
            payload = b'{"action":"record_artifact","payload":{}}'
            jobs = [asyncio.create_task(coordinator._act(payload, None)) for _ in range(2)]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                result = await asyncio.wait_for(
                    coordinator._act(b'{"action":"cancel_sequence","payload":{}}', None), 2
                )
                assert result.status == 204
                assert release.is_set() is False
            finally:
                release.set()
                await asyncio.gather(*jobs)

    asyncio.run(scenario())


def test_json_encoding_runs_in_the_worker_and_matches_existing_wire() -> None:
    caller_thread = threading.get_ident()
    threads: list[int] = []
    dumps = json.dumps

    def observed(value: object) -> str:
        threads.append(threading.get_ident())
        return dumps(value)

    async def scenario() -> None:
        with patch.object(json, "dumps", side_effect=observed):
            result = await seam.offload_route_handler(
                "authoring", lambda: seam.RouteResult(200, {"rows": ["a", "b"]})
            )
        assert result.wire == {"rows": ["a", "b"]}
        assert result._encoded_json == dumps(result.wire)
        assert threads and all(thread != caller_thread for thread in threads)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "module,decode,dispatch,register,result",
    [
        (
            sidebar,
            "decode_sidebar_action_json",
            "dispatch_sidebar_action",
            "ensure_sidebar_route_registered",
            {"ready": True},
        ),
        (
            production,
            "decode_production_action_json",
            "dispatch_production_action",
            "ensure_production_route_registered",
            SimpleNamespace(
                status=200, projection=SimpleNamespace(to_wire=lambda: {"ready": True})
            ),
        ),
        (
            authoring,
            "decode_authoring_action_json",
            "dispatch_authoring_action",
            "ensure_authoring_route_registered",
            SimpleNamespace(status=200, body={"ready": True}),
        ),
        (
            planning,
            "decode_production_planning_json",
            "dispatch_production_planning_action",
            "ensure_production_planning_route_registered",
            SimpleNamespace(status=200, to_wire=lambda: {"ready": True}),
        ),
    ],
)
def test_real_server_disconnect_overload_origin_and_unrelated_route(
    module: ModuleType,
    decode: str,
    dispatch: str,
    register: str,
    result: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aiohttp = pytest.importorskip("aiohttp")
    web = pytest.importorskip("aiohttp.web")
    # This repository-owned harness uses ComfyUI's handler_cancellation=False default.
    from test_m25_42_disconnected_route_responses import HostDefaultServer

    release, lock = threading.Event(), threading.Lock()
    started = 0
    commits: list[int] = []

    async def scenario() -> None:
        entered, completed = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        responses: list[Any] = []

        def blocking(_action: object) -> Any:
            nonlocal started
            with lock:
                started += 1
                number = started
                if started == 2:
                    loop.call_soon_threadsafe(entered.set)
            assert release.wait(8)
            commits.append(number)
            return result

        async def observes_response(request: Any, handler: Any) -> Any:
            response = await handler(request)
            response.headers.get("Content-Security-Policy")
            responses.append(response)
            if len([row for row in responses if row.status == 200]) == 2:
                completed.set()
            return response

        async def probe(_request: Any) -> Any:
            return web.Response(status=204)

        routes = web.RouteTableDef()
        monkeypatch.setitem(sys.modules, "server", host_prompt_server_module(routes))
        with (
            patch.object(module, decode, return_value={"action": "read_workspace"}),
            patch.object(module, dispatch, side_effect=blocking),
        ):
            assert getattr(module, register)()
            path = list(routes)[0].path
            app = web.Application(middlewares=[web.middleware(observes_response)])
            app.add_routes(routes)
            app.router.add_get("/worker-probe", probe)
            async with HostDefaultServer(app) as server:
                origin = f"http://{server.host}:{server.port}"
                request = (
                    f"POST {path} HTTP/1.1\r\nHost: {server.host}:{server.port}\r\n"
                    f"Origin: {origin}\r\nContent-Type: application/json\r\n"
                    "Content-Length: 2\r\n\r\n{}"
                ).encode()
                writers = []
                try:
                    for _ in range(2):
                        _, writer = await asyncio.open_connection(server.host, server.port)
                        writers.append(writer)
                        writer.write(request)
                        await writer.drain()
                    await asyncio.wait_for(entered.wait(), 3)
                    writers[0].close()
                    await writers[0].wait_closed()
                    async with aiohttp.ClientSession() as client:
                        async with client.get(server.make_url("/worker-probe")) as response:
                            assert response.status == 204
                        async with client.post(
                            server.make_url(path),
                            data=b"{}",
                            headers={"Origin": origin, "Content-Type": "application/json"},
                        ) as response:
                            assert response.status == 503
                            body = await response.read()
                            assert not body or json.loads(body) == {
                                "error": "route_worker_capacity"
                            }
                        async with client.post(
                            server.make_url(path),
                            data=b"{}",
                            headers={
                                "Origin": "http://foreign.invalid",
                                "Content-Type": "application/json",
                            },
                        ) as response:
                            assert response.status == 403
                    assert started == 2 and commits == [] and not release.is_set()
                finally:
                    release.set()
                    await asyncio.wait_for(completed.wait(), 5)
                    for writer in writers:
                        writer.close()
                        await writer.wait_closed()
                assert sorted(commits) == [1, 2]
                assert [row.status for row in responses].count(200) == 2

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert not [
        row
        for row in caplog.records
        if row.name.startswith("aiohttp") and row.levelno >= logging.ERROR
    ]
