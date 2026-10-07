"""M17-12 optional Production route ownership and empty-error-body checks."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_production_workspace as adapter
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    MAX_PRODUCTION_ACTION_BYTES,
    PRODUCTION_ACTION_ROUTE,
    ProductionDispatchResult,
    ProductionWorkbenchError,
)
from comfyui_h3_context.core import ProductionWorkbenchProjection
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_production_workspace.py"


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._done = False
        self.last_limit: int | None = None

    async def read(self, limit: int) -> bytes:
        self.last_limit = limit
        if self._done:
            return b""
        self._done = True
        return self._body


class _Headers:
    def __init__(self, origins: list[str]) -> None:
        self._origins = origins

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        return self._origins if name == "Origin" else default


def _web() -> SimpleNamespace:
    return SimpleNamespace(
        Response=lambda *, status: SimpleNamespace(status=status, body=None),
        json_response=lambda value, status=200, dumps=None: SimpleNamespace(
            status=status,
            body=value,
        ),
    )


def _register(routes: _Routes) -> Callable[..., Any]:
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = _web()
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False):
            if not adapter.ensure_production_route_registered():
                raise AssertionError("Production route was not registered")
            if not adapter.ensure_production_route_registered():
                raise AssertionError("Production route ownership was not idempotent")
    if len(routes) != 1:
        raise AssertionError("Production route registered more than once")
    return cast(Callable[..., Any], routes[0].handler)


def _action_bytes() -> bytes:
    return json.dumps(
        {
            "schema": "h3.context.production_workbench.action.v1",
            "request_id": "request.route",
            "action": "read_projection",
            "payload": {"workspace_handle": "pw_" + "a" * 43},
        }
    ).encode("utf-8")


def _request(
    body: bytes,
    *,
    origins: list[str] | None = None,
    content_type: str = "application/json",
    content_length: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content_type=content_type,
        content_length=len(body) if content_length is None else content_length,
        content=_Content(body),
        headers=_Headers(["http://127.0.0.1:8188"] if origins is None else origins),
        transport=ListenerTransport(),
    )


class ProductionRouteStaticTests(unittest.TestCase):
    def test_optional_imports_are_lazy_and_foreign_collision_fails_closed(self) -> None:
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
        top_imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top_imports.add(node.module.split(".", 1)[0])
        self.assertTrue({"aiohttp", "server"}.isdisjoint(top_imports))

        def foreign(_request: object) -> None:
            return None

        routes = _Routes(
            [SimpleNamespace(method="POST", path=PRODUCTION_ACTION_ROUTE, handler=foreign)]
        )
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertFalse(adapter.ensure_production_route_registered())
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].handler, foreign)


class ProductionRouteAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_media_type_and_declared_size_fail_with_empty_bodies(self) -> None:
        handler = _register(_Routes())
        cases = (
            (_request(_action_bytes(), origins=[]), 403),
            (_request(_action_bytes(), origins=["http://localhost:8188"]), 403),
            (_request(_action_bytes(), content_type="text/plain"), 415),
            (
                _request(
                    _action_bytes(),
                    content_length=MAX_PRODUCTION_ACTION_BYTES + 1,
                ),
                413,
            ),
        )
        for request, expected in cases:
            with patch.object(adapter, "dispatch_production_action") as dispatch:
                response = await handler(request)
            self.assertEqual((response.status, response.body), (expected, None))
            dispatch.assert_not_called()

    async def test_chunked_body_is_bounded_before_decode(self) -> None:
        handler = _register(_Routes())
        request = _request(b"x" * (MAX_PRODUCTION_ACTION_BYTES + 1))
        request.content_length = None
        response = await handler(request)
        self.assertEqual((response.status, response.body), (413, None))
        self.assertEqual(request.content.last_limit, MAX_PRODUCTION_ACTION_BYTES + 1)

    async def test_only_conflict_may_return_a_safe_projection_body(self) -> None:
        handler = _register(_Routes())
        projection = cast(
            ProductionWorkbenchProjection,
            SimpleNamespace(to_wire=lambda: {"schema": "safe.projection"}),
        )
        with patch.object(
            adapter,
            "dispatch_production_action",
            side_effect=ProductionWorkbenchError(
                "stale_workspace",
                409,
                projection=projection,
            ),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body, {"schema": "safe.projection"})

        with patch.object(
            adapter,
            "dispatch_production_action",
            side_effect=ProductionWorkbenchError("action_rejected", 422),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual((response.status, response.body), (422, None))

        with patch.object(
            adapter,
            "dispatch_production_action",
            return_value=ProductionDispatchResult(204),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual((response.status, response.body), (204, None))

        with patch.object(
            adapter,
            "dispatch_production_action",
            side_effect=RuntimeError("private host detail"),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual((response.status, response.body), (500, None))


if __name__ == "__main__":
    unittest.main()
