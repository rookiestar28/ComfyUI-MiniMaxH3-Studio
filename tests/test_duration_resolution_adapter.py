"""M23-04 deterministic duration-resolution adapter regressions."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_duration_resolution as adapter
from comfyui_h3_context.adapters.comfyui_duration_resolution import (
    DURATION_RESOLUTION_REQUEST_SCHEMA,
    DURATION_RESOLUTION_RESPONSE_SCHEMA,
    DURATION_RESOLUTION_ROUTE,
    MAX_DURATION_RESOLUTION_BYTES,
    DurationResolutionError,
    decode_duration_resolution_json,
    ensure_duration_resolution_route_registered,
    resolve_duration_request,
)
from comfyui_h3_context.core.length import MILLISECONDS_PER_SECOND, resolve_milliseconds
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_duration_resolution.py"


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class _Content:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = [*chunks, b""]

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0)


class _Headers:
    """The `getall` surface the shared route seam reads the origin through."""

    def __init__(self, origins: list[str]) -> None:
        self._origins = origins

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        return self._origins if name == "Origin" else default


# M23-47: this route reached the seam with no origin check of its own, so every request built here
# now has to carry the same header its nine siblings already required. The header below is the
# visible evidence of that fix; the invariant itself is asserted once, over every owned registered
# route, in tests/test_m23_47_route_seam.py.
def _request(
    body: bytes,
    *,
    content_type: str = "application/json",
    content_length: int | None | object = ...,  # sentinel means the real byte length
    origins: list[str] | None = None,
) -> SimpleNamespace:
    length = len(body) if content_length is ... else content_length
    return SimpleNamespace(
        content_type=content_type,
        content_length=length,
        content=_Content(body),
        headers=_Headers(["http://127.0.0.1:8188"] if origins is None else origins),
        transport=ListenerTransport(),
    )


def _registered_handler(routes: _Routes) -> Callable[..., Any]:
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200: (status, value)
    )
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False, create=True):
            assert ensure_duration_resolution_route_registered()
            assert ensure_duration_resolution_route_registered()
    assert len(routes) == 1
    assert routes[0].path == DURATION_RESOLUTION_ROUTE
    return cast(Callable[..., Any], routes[0].handler)


def test_eight_seconds_resolves_exactly_to_192_frames() -> None:
    assert resolve_duration_request(
        {
            "schema": DURATION_RESOLUTION_REQUEST_SCHEMA,
            "requested_seconds": 8,
        }
    ) == {
        "schema": DURATION_RESOLUTION_RESPONSE_SCHEMA,
        "requested_seconds": 8,
        "requested_milliseconds": 8000,
        "effective_milliseconds": 8000,
        "frame_count": 192,
        "snapped": False,
    }


def test_every_official_integer_delegates_to_the_core_length_authority() -> None:
    for requested_seconds in range(4, 16):
        expected = resolve_milliseconds(requested_seconds * MILLISECONDS_PER_SECOND)
        resolved = resolve_duration_request(
            {
                "schema": DURATION_RESOLUTION_REQUEST_SCHEMA,
                "requested_seconds": requested_seconds,
            }
        )
        assert resolved["requested_milliseconds"] == expected.requested_milliseconds
        assert resolved["effective_milliseconds"] == expected.delivered_milliseconds
        assert resolved["frame_count"] == expected.frame_count
        assert resolved["snapped"] == expected.snapped


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA},
        {"schema": "wrong", "requested_seconds": 8},
        {
            "schema": DURATION_RESOLUTION_REQUEST_SCHEMA,
            "requested_seconds": 8,
            "extra": True,
        },
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA, "requested_seconds": 3},
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA, "requested_seconds": 16},
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA, "requested_seconds": 8.0},
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA, "requested_seconds": True},
        {"schema": DURATION_RESOLUTION_REQUEST_SCHEMA, "requested_seconds": None},
    ],
)
def test_closed_request_rejects_non_product_values(payload: dict[str, object]) -> None:
    with pytest.raises(DurationResolutionError, match="^invalid_request$"):
        resolve_duration_request(payload)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{",
        b"\xff",
        b'{"schema":"h3.context.duration_resolution_request.v1",'
        b'"requested_seconds":8,"requested_seconds":9}',
    ],
)
def test_decoder_rejects_malformed_or_ambiguous_json(raw: bytes) -> None:
    with pytest.raises(DurationResolutionError, match="^invalid_request$"):
        decode_duration_resolution_json(raw)


def test_optional_host_imports_are_lazy_and_registration_is_idempotent() -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
    top_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_imports.add(node.module.split(".", 1)[0])
    assert {"aiohttp", "server"}.isdisjoint(top_imports)

    routes = _Routes()
    _registered_handler(routes)

    unavailable = host_prompt_server_module(object(), available=False)
    with patch.dict(sys.modules, {"server": unavailable}):
        assert not ensure_duration_resolution_route_registered()


def test_foreign_route_collision_fails_closed() -> None:
    def foreign(_request: object) -> None:
        return None

    routes = _Routes(
        [
            SimpleNamespace(
                method="POST",
                path=DURATION_RESOLUTION_ROUTE,
                handler=foreign,
            )
        ]
    )
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(json_response=lambda value, status=200: value)
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        assert not ensure_duration_resolution_route_registered()
    assert routes[0].handler is foreign


def test_route_bounds_and_resolves_the_closed_request() -> None:
    async def exercise() -> None:
        handler = _registered_handler(_Routes())

        status, value = await handler(_request(b"{}", content_type="text/plain"))
        assert (status, value) == (415, {"error": "invalid_request"})

        declared = _request(b"", content_length=MAX_DURATION_RESOLUTION_BYTES + 1)
        status, value = await handler(declared)
        assert (status, value) == (413, {"error": "request_too_large"})

        chunked = SimpleNamespace(
            content_type="application/json",
            content_length=None,
            content=_Content(b"x" * (MAX_DURATION_RESOLUTION_BYTES + 1)),
            headers=_Headers(["http://127.0.0.1:8188"]),
            transport=ListenerTransport(),
        )
        status, value = await handler(chunked)
        assert (status, value) == (413, {"error": "request_too_large"})

        body = json.dumps(
            {
                "schema": DURATION_RESOLUTION_REQUEST_SCHEMA,
                "requested_seconds": 15,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status, value = await handler(_request(body))
        assert status == 200
        assert value == {
            "schema": DURATION_RESOLUTION_RESPONSE_SCHEMA,
            "requested_seconds": 15,
            "requested_milliseconds": 15000,
            "effective_milliseconds": 15083,
            "frame_count": 362,
            "snapped": True,
        }

    asyncio.run(exercise())
