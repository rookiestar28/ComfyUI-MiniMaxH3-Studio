"""A completed worker wakes its route without waiting out the observation interval."""

from __future__ import annotations

import asyncio
import gc
import threading
from typing import Any

import pytest
from test_m25_media_derivative_contract import create_wire
from test_m25_media_derivative_routes import same_origin
from test_m25_media_source_leases import Claim, command, request

from comfyui_h3_context.adapters import comfyui_authoring_media_leases as routes
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority

web = pytest.importorskip("aiohttp.web")
http_test = pytest.importorskip("aiohttp.test_utils")


@pytest.mark.parametrize("operation", ("create", "open", "renew", "transfer"))
@pytest.mark.parametrize("worker_fails", (False, True))
@pytest.mark.parametrize("wrapper_cancelled", (False, True))
def test_completion_wakes_each_worker_request_without_fixed_sleep(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    worker_fails: bool,
    wrapper_cancelled: bool,
) -> None:
    async def run() -> None:
        authority = MediaLeaseAuthority(lambda _: Claim(), start_reaper=False)
        service = routes.MediaLeaseRouteService(authority)
        started, released = threading.Event(), threading.Event()
        original_work = service._work
        original_wait = asyncio.wait
        original_wrap = asyncio.wrap_future
        waits: list[float | None] = []
        loop_errors: list[dict[str, Any]] = []
        handler_finished = asyncio.Event()
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, detail: loop_errors.append(detail)
        )

        def work(*args: Any) -> Any:
            started.set()
            if not released.wait(2):
                raise RuntimeError("worker was never joined")
            if worker_fails:
                raise RuntimeError("controlled worker fault")
            return original_work(*args)

        async def waiting(futures: Any, *, timeout: float | None = None, **kwargs: Any) -> Any:
            assert started.wait(1)
            assert service.claimed(), "the pending worker owns the route claim"
            waits.append(timeout)
            released.set()
            return await original_wait(futures, timeout=timeout, **kwargs)

        async def fixed_sleep(_seconds: float, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("route slept instead of awaiting worker completion")

        def wrap(future: Any) -> asyncio.Future[Any]:
            assert started.wait(1)
            completion = original_wrap(future)
            if wrapper_cancelled:
                completion.cancel()
            return completion

        monkeypatch.setattr(service, "_work", work)
        app = web.Application()

        def completing(handler: Any) -> Any:
            async def wrapped(request: Any) -> Any:
                try:
                    return await handler(request)
                finally:
                    handler_finished.set()

            return wrapped

        app.router.add_post(routes.LEASE_ROUTE, completing(service.control))
        app.router.add_post(routes.LEASE_ROUTE + "/open", completing(service.open))
        try:
            async with http_test.TestClient(http_test.TestServer(app)) as client:
                headers = same_origin(client)
                if operation == "create":
                    body = create_wire()
                else:
                    receipt, capability = authority.create(request())
                    body = command(
                        receipt,
                        operation,
                        **(
                            {"next_owner_id": "owner-next", "next_runtime_epoch": 2}
                            if operation == "transfer"
                            else {}
                        ),
                    ).to_wire()
                    headers[routes.CAPABILITY_HEADER] = capability
                with monkeypatch.context() as patch:
                    patch.setattr(asyncio, "wait", waiting)
                    patch.setattr(asyncio, "sleep", fixed_sleep)
                    patch.setattr(asyncio, "wrap_future", wrap)
                    response = await client.post(
                        routes.LEASE_ROUTE + ("/open" if operation == "open" else ""),
                        json=body,
                        headers=headers,
                    )
                    assert response.status == (500 if worker_fails else 200)
                    await response.read()
                assert waits and set(waits) == {0.05}
                # IMPORTANT: client EOF can precede server write_eof/finally cleanup.
                # Observe actual handler completion before asserting its claim was released.
                await asyncio.wait_for(handler_finished.wait(), timeout=1)
                assert not service.claimed()
                assert loop_errors == []
        finally:
            released.set()
            service.close()

    asyncio.run(run())


def test_abandoned_worker_failure_is_observed_and_keeps_claim_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        authority = MediaLeaseAuthority(lambda _: Claim(), start_reaper=False)
        service = routes.MediaLeaseRouteService(authority)
        started, released = threading.Event(), threading.Event()
        cleanup_finished = threading.Event()
        errors: list[dict[str, Any]] = []
        wrappers: list[asyncio.Future[Any]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, detail: errors.append(detail))
        original_wrap = asyncio.wrap_future
        original_abandoned = service._abandoned

        def abandoned(future: Any) -> None:
            try:
                original_abandoned(future)
            finally:
                cleanup_finished.set()

        monkeypatch.setattr(service, "_abandoned", abandoned)

        def wrap(*args: Any, **kwargs: Any) -> asyncio.Future[Any]:
            result = original_wrap(*args, **kwargs)
            wrappers.append(result)
            return result

        def work(*_args: Any) -> Any:
            started.set()
            assert released.wait(2)
            raise RuntimeError("late abandoned worker fault")

        monkeypatch.setattr(service, "_work", work)
        monkeypatch.setattr(service, "_closing", lambda _request: started.is_set())
        app = web.Application()
        app.router.add_post(routes.LEASE_ROUTE, service.control)
        try:
            async with http_test.TestClient(http_test.TestServer(app)) as client:
                with monkeypatch.context() as patch:
                    patch.setattr(asyncio, "wrap_future", wrap)
                    response = await client.post(
                        routes.LEASE_ROUTE, json=create_wire(), headers=same_origin(client)
                    )
                    assert response.status == 499
                    await response.read()
                assert service.claimed(), "abandonment cannot free a running worker"
                assert len(wrappers) == 1
                released.set()
                await asyncio.wait(wrappers, timeout=2)
                assert wrappers[0].done()
                # Worker-result delivery and abandoned-claim cleanup are separate callbacks.
                # Join cleanup itself; event-loop turns cannot prove the worker thread finished it.
                assert await asyncio.to_thread(cleanup_finished.wait, 2)
                for _ in range(3):
                    await asyncio.sleep(0)
                assert not service.claimed()
                wrappers.clear()
                gc.collect()
                await asyncio.sleep(0)
                assert errors == [], "the completion wrapper must consume abandoned faults"
                assert authority.resources()["leases"] == 0
        finally:
            released.set()
            service.close()

    asyncio.run(run())
