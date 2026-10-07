from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from test_m25_media_derivative_contract import create_wire
from test_m25_media_source_leases import Claim

from comfyui_h3_context.adapters import comfyui_authoring_media_leases as route_module
from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.adapters.comfyui_authoring_media_leases import (
    CAPABILITY_HEADER,
    GEOMETRY_HEADER,
    LEASE_ROUTE,
    MediaLeaseRouteService,
)
from comfyui_h3_context.core.authoring_media import (
    REQUEST_SCHEMA,
    CreateLeaseRequest,
    MediaGeometry,
    VerifiedDerivative,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

web = pytest.importorskip("aiohttp.web")
http_test = pytest.importorskip("aiohttp.test_utils")
TestClient, TestServer = http_test.TestClient, http_test.TestServer


def same_origin(client: Any) -> dict[str, str]:
    """The page origin a browser on this test server would send (M23-57: never a fixed one)."""
    authority = f"{client.host}:{client.port}"
    return {"Origin": f"http://{authority}", "Host": authority}


def test_real_http_lease_lifecycle_and_origin_guards() -> None:
    async def run() -> None:
        class GeometryClaim(Claim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                body, facts = super().generate(deadline, cancellation)
                return body, replace(facts, geometry=MediaGeometry(1920, 1080, 320, 180))

        authority = MediaLeaseAuthority(lambda _: GeometryClaim(), start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        app.router.add_post(LEASE_ROUTE + "/open", service.open)
        async with TestClient(TestServer(app)) as client:
            headers = same_origin(client)
            denied = await client.post(LEASE_ROUTE, json=create_wire())
            assert denied.status == 403
            created = await client.post(LEASE_ROUTE, json=create_wire(), headers=headers)
            assert created.status == 200
            receipt = await created.json()
            capability = created.headers[CAPABILITY_HEADER]
            assert capability not in json.dumps(receipt)
            assert set(receipt) == {
                "schema",
                "requestId",
                "operation",
                "leaseId",
                "revision",
                "ownerId",
                "runtimeEpoch",
                "ttlMs",
                "derivativeKind",
                "mediaType",
                "byteCount",
                "derivativeFingerprint",
                "profileFingerprint",
                "audioDisposition",
                "assetFingerprint",
                "derivativeProfileId",
            }

            def command(operation: str) -> dict[str, object]:
                return dict(
                    schema=REQUEST_SCHEMA,
                    operation=operation,
                    requestId="http-op",
                    **{
                        key: receipt[key]
                        for key in ("leaseId", "revision", "ownerId", "runtimeEpoch")
                    },
                )

            private = dict(headers, **{CAPABILITY_HEADER: capability})
            unfenced = await client.post(
                LEASE_ROUTE + "/open", json=command("open"), headers=headers
            )
            assert unfenced.status == 410
            assert GEOMETRY_HEADER not in unfenced.headers
            opened = await client.post(LEASE_ROUTE + "/open", json=command("open"), headers=private)
            assert opened.status == 200
            assert await opened.read() == b"synthetic-state-machine-body"
            assert opened.headers["Cache-Control"] == "no-store"
            assert (
                opened.headers["X-H3-Context-Media-Derivative"] == receipt["derivativeFingerprint"]
            )
            assert json.loads(opened.headers[GEOMETRY_HEADER]) == {
                "schema": "h3.authoring.media_geometry.v1",
                "sourceWidth": 1920,
                "sourceHeight": 1080,
                "derivativeWidth": 320,
                "derivativeHeight": 180,
            }
            renewed = await client.post(LEASE_ROUTE, json=command("renew"), headers=private)
            assert renewed.status == 200
            assert CAPABILITY_HEADER not in renewed.headers
            receipt = await renewed.json()
            transfer = command("transfer") | {"nextOwnerId": "owner-next", "nextRuntimeEpoch": 2}
            moved = await client.post(LEASE_ROUTE, json=transfer, headers=private)
            assert moved.status == 200
            old_release = command("release")
            receipt = await moved.json()
            rejected = await client.post(LEASE_ROUTE, json=old_release, headers=private)
            assert rejected.status == 410
            private[CAPABILITY_HEADER] = moved.headers[CAPABILITY_HEADER]
            released = await client.post(LEASE_ROUTE, json=command("release"), headers=private)
            assert released.status == 200
            revoked = await client.post(
                LEASE_ROUTE + "/open", json=command("open"), headers=private
            )
            assert revoked.status == 410
            assert GEOMETRY_HEADER not in revoked.headers
            assert authority.resources()["cacheBytes"] == 0
        service.close()

    asyncio.run(run())


def test_http_open_serializes_absent_geometry_as_null_header() -> None:
    async def run() -> None:
        authority = MediaLeaseAuthority(lambda _: Claim(), start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        app.router.add_post(LEASE_ROUTE + "/open", service.open)
        async with TestClient(TestServer(app)) as client:
            headers = same_origin(client)
            created = await client.post(LEASE_ROUTE, json=create_wire(), headers=headers)
            receipt = await created.json()
            capability = created.headers[CAPABILITY_HEADER]
            command = {
                "schema": REQUEST_SCHEMA,
                "operation": "open",
                "requestId": "http-null-geometry",
                **{key: receipt[key] for key in ("leaseId", "revision", "ownerId", "runtimeEpoch")},
            }
            opened = await client.post(
                LEASE_ROUTE + "/open",
                json=command,
                headers=dict(headers, **{CAPABILITY_HEADER: capability}),
            )
            assert opened.status == 200
            assert opened.headers[GEOMETRY_HEADER] == "null"
            await opened.read()
        service.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "host",
        "duplicate_origin",
        "range",
        "query",
        "encoding",
        "oversized",
        "unknown_key",
        "capability_on_create",
        "open_on_control",
    ],
)
def test_http_refusals_never_admit_private_source(case: str) -> None:
    async def run() -> None:
        admissions: list[CreateLeaseRequest] = []

        def factory(request: CreateLeaseRequest) -> Claim:
            admissions.append(request)
            return Claim()

        authority = MediaLeaseAuthority(factory, start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        async with TestClient(TestServer(app)) as client:
            headers = [
                *same_origin(client).items(),
                ("Content-Type", "application/json"),
            ]
            wire = create_wire()
            url = LEASE_ROUTE
            if case == "host":
                headers[1] = ("Host", "foreign.invalid")
            elif case == "duplicate_origin":
                headers.append(headers[0])
            elif case == "range":
                headers.append(("Range", "bytes=0-1"))
            elif case == "query":
                url += "?lease=opaque"
            elif case == "encoding":
                headers.append(("Content-Encoding", "identity"))
            elif case == "unknown_key":
                wire["path"] = "private-sentinel"
            elif case == "capability_on_create":
                headers.append((CAPABILITY_HEADER, "a" * 64))
            elif case == "open_on_control":
                wire = dict(
                    schema=REQUEST_SCHEMA,
                    operation="open",
                    requestId="bad-op",
                    leaseId="lease-1",
                    ownerId="owner-1",
                    revision=1,
                    runtimeEpoch=1,
                )
            data = b"x" * 8193 if case == "oversized" else json.dumps(wire).encode()
            response = await client.post(url, data=data, headers=headers)
            assert response.status in (400, 403, 413)
            assert "private-sentinel" not in await response.text()
            assert not admissions
        service.close()

    asyncio.run(run())


def test_disconnect_keeps_worker_claim_until_cancelled_generation_finishes() -> None:
    async def run() -> None:
        started = threading.Event()
        finished = threading.Event()

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
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        async with TestClient(TestServer(app)) as client:
            headers = same_origin(client)
            pending = asyncio.create_task(
                client.post(LEASE_ROUTE, json=create_wire(), headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert started.is_set()
            refused = await client.post(LEASE_ROUTE, json=create_wire(), headers=headers)
            assert refused.status == 429
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            while not finished.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert finished.is_set()
            while service._claim.locked() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert not service._claim.locked()
            assert authority.resources()["cacheBytes"] == 0
            assert authority.resources()["leases"] == 0
        service.close()

    asyncio.run(run())


def test_clip_playback_waits_for_abandoned_asset_generation_handoff() -> None:
    async def run() -> None:
        started = threading.Event()
        allow_release = threading.Event()

        class BlockingDecorationClaim(Claim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                started.set()
                while not cancellation.is_cancelled() and time.monotonic() < deadline:
                    time.sleep(0.01)
                while not allow_release.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return super().generate(deadline, cancellation)

        def factory(request: CreateLeaseRequest) -> Claim:
            return BlockingDecorationClaim() if request.scope == "asset" else Claim()

        authority = MediaLeaseAuthority(factory, start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        async with TestClient(TestServer(app)) as client:
            headers = same_origin(client)
            decoration = create_wire()
            decoration.update(
                requestId="decoration-request",
                scope="asset",
                clipId=None,
                derivativeKind="thumbnail",
                ownerId="decoration-1",
                sourceEndFrame=1,
            )
            pending = asyncio.create_task(
                client.post(LEASE_ROUTE, json=decoration, headers=headers)
            )
            cutoff = time.monotonic() + 3
            while not started.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert started.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            playback = create_wire()
            playback["requestId"] = "playback-request"
            waiting = asyncio.create_task(client.post(LEASE_ROUTE, json=playback, headers=headers))
            cutoff = time.monotonic() + 3
            while not service._playback_handoff.locked() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert service._playback_handoff.locked()

            competing = create_wire()
            competing["requestId"] = "competing-playback-request"
            refused = await client.post(LEASE_ROUTE, json=competing, headers=headers)
            assert refused.status == 429
            assert (await refused.json())["reason"] == "busy"

            allow_release.set()
            admitted = await waiting
            assert admitted.status == 200
            assert (await admitted.json())["requestId"] == "playback-request"
        service.close()

    asyncio.run(run())


def test_clip_playback_waits_for_abandoned_asset_open_handoff() -> None:
    async def run() -> None:
        opened = threading.Event()
        allow_release = threading.Event()

        class ThumbnailClaim(Claim):
            def generate(
                self, deadline: float, cancellation: Any
            ) -> tuple[bytearray, VerifiedDerivative]:
                body, facts = super().generate(deadline, cancellation)
                return body, replace(
                    facts,
                    kind="thumbnail",
                    geometry=MediaGeometry(64, 36, 64, 36),
                )

        class BlockingOpenAuthority(MediaLeaseAuthority):
            def open(
                self, command: Any, capability: str, *, cancellation: object | None = None
            ) -> Any:
                opened.set()
                deadline = time.monotonic() + 3
                checker = getattr(cancellation, "is_cancelled", None)
                while not callable(checker) or not checker():
                    if time.monotonic() >= deadline:
                        pytest.fail("open request did not receive cancellation")
                    time.sleep(0.01)
                while not allow_release.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return super().open(command, capability, cancellation=cancellation)

        authority = BlockingOpenAuthority(lambda _: ThumbnailClaim(), start_reaper=False)
        service = MediaLeaseRouteService(authority)
        app = web.Application()
        app.router.add_post(LEASE_ROUTE, service.control)
        app.router.add_post(LEASE_ROUTE + "/open", service.open)
        async with TestClient(TestServer(app)) as client:
            headers = same_origin(client)
            decoration = create_wire()
            decoration.update(
                requestId="open-decoration-request",
                scope="asset",
                clipId=None,
                derivativeKind="thumbnail",
                ownerId="open-decoration-1",
                sourceEndFrame=1,
            )
            created = await client.post(LEASE_ROUTE, json=decoration, headers=headers)
            assert created.status == 200
            receipt = await created.json()
            capability = created.headers[CAPABILITY_HEADER]
            open_command = {
                "schema": REQUEST_SCHEMA,
                "operation": "open",
                "requestId": "open-decoration-body",
                **{key: receipt[key] for key in ("leaseId", "revision", "ownerId", "runtimeEpoch")},
            }
            pending = asyncio.create_task(
                client.post(
                    LEASE_ROUTE + "/open",
                    json=open_command,
                    headers=dict(headers, **{CAPABILITY_HEADER: capability}),
                )
            )
            cutoff = time.monotonic() + 3
            while not opened.is_set() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert opened.is_set()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            playback = create_wire()
            playback["requestId"] = "open-handoff-playback-request"
            waiting = asyncio.create_task(client.post(LEASE_ROUTE, json=playback, headers=headers))
            cutoff = time.monotonic() + 3
            while not service._playback_handoff.locked() and time.monotonic() < cutoff:
                await asyncio.sleep(0.01)
            assert service._playback_handoff.locked()
            allow_release.set()
            admitted = await waiting
            assert admitted.status == 200
            assert (await admitted.json())["requestId"] == "open-handoff-playback-request"
        service.close()

    asyncio.run(run())


def test_actual_route_table_registration_is_owned_idempotent_and_refuses_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = web.RouteTableDef()
    monkeypatch.setitem(
        sys.modules,
        "server",
        host_prompt_server_module(routes),
    )
    service = MediaLeaseRouteService(MediaLeaseAuthority(lambda _: Claim(), start_reaper=False))
    monkeypatch.setattr(route_module, "_SERVICE", service)
    try:
        assert route_module.ensure_authoring_media_lease_routes_registered()
        assert route_module.ensure_authoring_media_lease_routes_registered()
        assert len(routes) == 2
        assert {row.path for row in routes} == {LEASE_ROUTE, LEASE_ROUTE + "/open"}
        partial = web.RouteTableDef()
        partial.post(LEASE_ROUTE)(routes[0].handler)
        monkeypatch.setitem(
            sys.modules,
            "server",
            host_prompt_server_module(partial),
        )
        assert not route_module.ensure_authoring_media_lease_routes_registered()
        assert len(partial) == 1
    finally:
        service.close()
