"""M23-15 lazy route ownership and closed transport checks."""

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

import comfyui_h3_context.adapters.comfyui_sequence_coordinator as adapter
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_ACTION_ROUTE,
    COORDINATOR_ACTION_SCHEMA,
    COORDINATOR_ERROR_SCHEMA,
    MAX_COORDINATOR_ACTION_BYTES,
    SequenceCoordinatorDispatchResult,
    SequenceCoordinatorError,
)
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_sequence_coordinator.py"


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
            status=status, body=value
        ),
    )


def _register(routes: _Routes) -> Callable[..., Any]:
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = _web()
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(adapter, "_ROUTE_REGISTERED", False):
            assert adapter.ensure_sequence_coordinator_route_registered()
            assert adapter.ensure_sequence_coordinator_route_registered()
    assert len(routes) == 1
    return cast(Callable[..., Any], routes[0].handler)


def _action_bytes() -> bytes:
    return json.dumps(
        {
            "schema": COORDINATOR_ACTION_SCHEMA,
            "request_id": "coordinator.route.read",
            "action": "read_sequence",
            "payload": {"run_handle": "mc_" + "m" * 40},
        }
    ).encode("utf-8")


def _prepare_action_bytes() -> bytes:
    fingerprint = "sha256:" + "a" * 64
    return json.dumps(
        {
            "schema": COORDINATOR_ACTION_SCHEMA,
            "request_id": "coordinator.route.prepare",
            "action": "prepare_sequence",
            "payload": {
                "workspace_handle": "pw_" + "w" * 40,
                "expected_workspace_revision": 1,
                "expected_workspace_fingerprint": fingerprint,
                "correlation": {
                    "prompt_id": "prompt.route.prepare",
                    "execution_node_id": "147",
                },
                "observation": {
                    "schema": "h3.context.prepared_graph_observation.v4",
                    "route": "replace",
                    "graph_fingerprint": fingerprint,
                    "compiled_prompt_fingerprint": fingerprint,
                    "owned_projection_fingerprint": fingerprint,
                    "owned_node_ids": [str(index) for index in range(1, 9)],
                    "owned_link_ids": [str(index) for index in range(11, 21)],
                    "model_fingerprint": fingerprint,
                    "runtime_fingerprint": fingerprint,
                    "fingerprint_domain": "output_producing_graph",
                    "expected_frames": 192,
                    "source_identity": None,
                    "timeout_ms": 3_600_000,
                    "native_anchor_node_id": "140:131",
                },
            },
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


class SequenceCoordinatorRouteStaticTests(unittest.TestCase):
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
            [SimpleNamespace(method="POST", path=COORDINATOR_ACTION_ROUTE, handler=foreign)]
        )
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertFalse(adapter.ensure_sequence_coordinator_route_registered())
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].handler, foreign)


class SequenceCoordinatorRouteAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_identity_arrays_reach_the_coordinator_worker(self) -> None:
        handler = _register(_Routes())
        with patch.object(
            adapter,
            "dispatch_sequence_coordinator_action",
            return_value=SequenceCoordinatorDispatchResult(204),
        ) as dispatch:
            response = await handler(_request(_prepare_action_bytes()))
        self.assertEqual((response.status, response.body), (204, None))
        dispatch.assert_called_once()

    async def test_origin_media_type_and_declared_size_fail_with_empty_bodies(self) -> None:
        handler = _register(_Routes())
        cases = (
            (_request(_action_bytes(), origins=[]), 403),
            (_request(_action_bytes(), origins=["http://localhost:8188"]), 403),
            (_request(_action_bytes(), content_type="text/plain"), 415),
            (
                _request(
                    _action_bytes(),
                    content_length=MAX_COORDINATOR_ACTION_BYTES + 1,
                ),
                413,
            ),
        )
        for request, expected in cases:
            with patch.object(adapter, "dispatch_sequence_coordinator_action") as dispatch:
                response = await handler(request)
            self.assertEqual((response.status, response.body), (expected, None))
            dispatch.assert_not_called()

    async def test_chunked_body_and_duplicate_members_are_rejected_before_dispatch(self) -> None:
        handler = _register(_Routes())
        oversized = _request(b"x" * (MAX_COORDINATOR_ACTION_BYTES + 1))
        oversized.content_length = None
        response = await handler(oversized)
        self.assertEqual((response.status, response.body), (413, None))
        self.assertEqual(oversized.content.last_limit, MAX_COORDINATOR_ACTION_BYTES + 1)

        duplicate = _request(
            b'{"schema":"x","schema":"y","request_id":"r","action":"read_sequence","payload":{}}'
        )
        with patch.object(adapter, "dispatch_sequence_coordinator_action") as dispatch:
            response = await handler(duplicate)
        self.assertEqual((response.status, response.body), (400, None))
        dispatch.assert_not_called()

    async def test_valid_body_reaches_one_worker_and_returns_only_closed_safe_errors(self) -> None:
        handler = _register(_Routes())
        with patch.object(
            adapter,
            "dispatch_sequence_coordinator_action",
            return_value=SequenceCoordinatorDispatchResult(204),
        ) as dispatch:
            response = await handler(_request(_action_bytes()))
        self.assertEqual((response.status, response.body), (204, None))
        dispatch.assert_called_once()

        with patch.object(
            adapter,
            "dispatch_sequence_coordinator_action",
            side_effect=SequenceCoordinatorError(
                "artifact_shape_mismatch",
                422,
                retry_disposition="inspect_native",
                same_run_authority=True,
            ),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual(
            (response.status, response.body),
            (
                422,
                {
                    "schema": COORDINATOR_ERROR_SCHEMA,
                    "category": "artifact_content_invalid",
                    "retry_disposition": "inspect_native",
                    "same_run_authority": True,
                },
            ),
        )

        with patch.object(
            adapter,
            "dispatch_sequence_coordinator_action",
            side_effect=RuntimeError("private host path"),
        ):
            response = await handler(_request(_action_bytes()))
        self.assertEqual(
            (response.status, response.body),
            (
                500,
                {
                    "schema": COORDINATOR_ERROR_SCHEMA,
                    "category": "internal_failure",
                    "retry_disposition": "none",
                    "same_run_authority": False,
                },
            ),
        )
        self.assertNotIn("private host path", json.dumps(response.body))


if __name__ == "__main__":
    unittest.main()
