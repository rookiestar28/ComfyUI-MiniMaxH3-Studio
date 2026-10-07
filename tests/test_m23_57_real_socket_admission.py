"""M23-57 F01: admission reads the socket that really accepted the connection.

The other M23-57 suites hand the seam a transport double. Here the same owned routes are served by a
real aiohttp listener on an ephemeral loopback port, so `request.transport.get_extra_info` is the
event loop's own transport and the port is one no fixture chose. Skipped where aiohttp is absent,
like the other real-server route suites; the supplied host is the actual-host proof (plan F03).
"""

from __future__ import annotations

import asyncio
import socket
import sys
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

web = pytest.importorskip("aiohttp.web")
aiohttp_client = pytest.importorskip("aiohttp")

from comfyui_h3_context.adapters import comfyui_build_provenance as provenance  # noqa: E402
from comfyui_h3_context.adapters import comfyui_duration_resolution as duration  # noqa: E402
from comfyui_h3_context.adapters import comfyui_route_seam as seam  # noqa: E402
from comfyui_h3_context.adapters.comfyui_build_provenance import (  # noqa: E402
    BUILD_PROVENANCE_ROUTE,
)
from comfyui_h3_context.adapters.comfyui_duration_resolution import (  # noqa: E402
    DURATION_RESOLUTION_ROUTE,
)
from comfyui_h3_context.core.request_target import AdmissionReason  # noqa: E402
from scripts.hc_09_host_seam_test_double import host_prompt_server_module  # noqa: E402


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


async def _serve(address: str) -> tuple[Any, int]:
    table = web.RouteTableDef()
    real_aiohttp = sys.modules["aiohttp"]
    with patch.dict(
        sys.modules, {"server": host_prompt_server_module(table), "aiohttp": real_aiohttp}
    ):
        for module, registrar in (
            (provenance, provenance.ensure_build_provenance_route_registered),
            (duration, duration.ensure_duration_resolution_route_registered),
        ):
            with patch.object(module, "_ROUTE_REGISTERED", False):
                assert registrar()
    app = web.Application()
    app.add_routes(table)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, address, 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, port


async def _exchange(
    address: str, port: int, method: str, path: str, headers: dict[str, str]
) -> tuple[int, list[AdmissionReason]]:
    noted: list[AdmissionReason] = []
    host = f"[{address}]" if ":" in address else address
    url = f"http://{host}:{port}{path}"
    with patch.object(seam, "_note_refusal", noted.append):
        async with aiohttp_client.ClientSession() as session:
            async with session.request(
                method,
                url,
                headers=headers,
                data=b"{}" if method == "POST" else None,
            ) as response:
                await response.read()
                return response.status, noted


def _cases(address: str, port: int) -> list[tuple[str, str, dict[str, str], list[AdmissionReason]]]:
    literal = f"[{address}]" if ":" in address else address
    own = f"http://{literal}:{port}"
    json = {"Content-Type": "application/json"}
    return [
        # The page served by this very listener, addressed by its IP literal and by `localhost`.
        ("POST", DURATION_RESOLUTION_ROUTE, {**json, "Origin": own}, []),
        (
            "POST",
            DURATION_RESOLUTION_ROUTE,
            {**json, "Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"},
            [],
        ),
        ("GET", BUILD_PROVENANCE_ROUTE, {}, []),
        # The pre-M23-57 pinned origin is foreign to a listener on another port.
        (
            "POST",
            DURATION_RESOLUTION_ROUTE,
            {**json, "Origin": "http://127.0.0.1:8188"},
            [AdmissionReason.ORIGIN_MISMATCH],
        ),
        # A rebinding-shaped request: Host and Origin agree on a name this socket is not.
        (
            "POST",
            DURATION_RESOLUTION_ROUTE,
            {**json, "Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"},
            [AdmissionReason.HOST_UNTRUSTED],
        ),
        (
            "GET",
            BUILD_PROVENANCE_ROUTE,
            {"Host": f"evil.example:{port}"},
            [AdmissionReason.HOST_UNTRUSTED],
        ),
    ]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        pytest.param(
            "::1",
            marks=pytest.mark.skipif(
                not _ipv6_loopback_available(), reason="IPv6 loopback is unavailable"
            ),
        ),
    ],
)
def test_a_real_listener_admits_its_own_page_on_its_own_port(address: str) -> None:
    async def scenario() -> None:
        runner, port = await _serve(address)
        try:
            assert port != 8188
            for method, path, headers, refused in _cases(address, port):
                status, noted = await _exchange(address, port, method, path, headers)
                assert noted == refused, (method, path, headers)
                if refused:
                    assert status == 403
                else:
                    assert status != 403
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_the_listener_is_the_real_aiohttp() -> None:
    # The cases above are only F01 evidence if the transport is the event loop's, not a double.
    assert isinstance(sys.modules["aiohttp"], ModuleType)
    assert hasattr(sys.modules["aiohttp"], "ClientSession")
