"""Read-only status, optional registration, collision/origin and privacy contracts."""

from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from deployment_request_doubles import ListenerTransport, admission_headers

from scripts.hc_09_host_seam_test_double import host_prompt_server_module

WIRE = {
    "schema": "h3.context.perception_host_status.v1",
    "state": "configured",
    "visual": True,
    "audio": False,
    "reason": "none",
    "cancellation_observed": False,
}


class _Routes(list[SimpleNamespace]):
    def get(self, path: str, *, allow_head: bool = True) -> Any:
        def decorate(handler: Any) -> Any:
            self.append(SimpleNamespace(method="GET", path=path, handler=handler))
            return handler

        return decorate


class _Headers:
    def __init__(self, origins: list[str]) -> None:
        self.values = admission_headers(origins)

    def getall(self, name: str, default: list[str]) -> list[str]:
        return list(self.values.get(name, default))


class _Unreadable:
    async def read(self, _limit: int) -> bytes:
        raise AssertionError("read_only_status_read_request_body")


class PerceptionHostStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.route = importlib.import_module(
            "comfyui_h3_context.adapters.comfyui_perception_status"
        )
        self.routes = _Routes()
        self.server = host_prompt_server_module(self.routes)
        self.aiohttp = ModuleType("aiohttp")
        self.aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda wire, status=200, **kwargs: SimpleNamespace(
                body=wire, status=status
            ),
            Response=lambda status=200, **kwargs: SimpleNamespace(body=None, status=status),
        )
        registered = patch.object(self.route, "_ROUTE_REGISTERED", False)
        registered.start()
        self.addCleanup(registered.stop)

    def host(self) -> Any:
        return patch.dict(sys.modules, {"server": self.server, "aiohttp": self.aiohttp})

    def register(self) -> Any:
        self.assertTrue(
            self.route.ensure_perception_host_status_route_registered(),
            "perception_status_get_not_registered",
        )
        self.assertEqual(len(self.routes), 1)
        self.assertEqual(
            (self.routes[0].method, self.routes[0].path),
            ("GET", "/h3-context/v1/perception/status"),
        )
        return self.routes[0].handler

    def request(self, origins: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            headers=_Headers(origins),
            transport=ListenerTransport(),
            content=_Unreadable(),
            content_length=1,
            content_type="text/plain",
            query_string="",
        )

    async def test_direct_handler_returns_only_the_current_cached_wire(self) -> None:
        with patch.object(
            self.route, "read_perception_host_status", return_value=dict(WIRE)
        ) as read:
            result = await self.route._status(b"", None)
        self.assertEqual(result.status, 200, "perception_status_cached_read_not_implemented")
        self.assertEqual(result.wire, WIRE)
        read.assert_called_once_with()

    async def test_optional_host_absence_has_no_registration_or_file_io(self) -> None:
        with (
            patch.dict(sys.modules, {"server": None, "aiohttp": None}),
            patch.object(
                Path, "open", side_effect=AssertionError("status_registration_read_settings")
            ),
        ):
            self.assertFalse(self.route.ensure_perception_host_status_route_registered())
        self.assertEqual(self.routes, [])

    async def test_same_origin_or_absent_get_reads_cached_status_without_file_or_body(self) -> None:
        with (
            self.host(),
            patch.object(
                self.route, "read_perception_host_status", return_value=dict(WIRE)
            ) as read,
        ):
            handler = self.register()
            self.assertTrue(self.route.ensure_perception_host_status_route_registered())
            for origins in ([], ["http://127.0.0.1:8188"]):
                with patch.object(
                    Path, "open", side_effect=AssertionError("status_read_private_file")
                ):
                    response = await handler(self.request(origins))
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body, WIRE)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(len(self.routes), 1)

    async def test_foreign_or_duplicate_origin_refuses_before_status_or_request_body(self) -> None:
        with (
            self.host(),
            patch.object(
                self.route,
                "read_perception_host_status",
                side_effect=AssertionError("foreign_status_read"),
            ) as read,
        ):
            handler = self.register()
            for origins in (
                ["http://foreign.invalid"],
                ["http://localhost:8188"],
                ["http://127.0.0.1:8188", "http://127.0.0.1:8188"],
            ):
                self.assertEqual((await handler(self.request(origins))).status, 403)
            read.assert_not_called()

    async def test_foreign_collision_is_preserved(self) -> None:
        foreign = object()
        self.routes.append(
            SimpleNamespace(method="GET", path="/h3-context/v1/perception/status", handler=foreign)
        )
        with self.host():
            self.assertFalse(self.route.ensure_perception_host_status_route_registered())
        self.assertEqual(len(self.routes), 1)
        self.assertIs(self.routes[0].handler, foreign)

    async def test_status_exception_is_a_finite_refusal_without_private_details(self) -> None:
        with (
            self.host(),
            patch.object(
                self.route,
                "read_perception_host_status",
                side_effect=OSError("untrusted_operator_value"),
            ),
        ):
            handler = self.register()
            response = await handler(self.request([]))
        self.assertEqual(response.status, 500)
        self.assertNotIn("untrusted_operator_value", json.dumps(response.body))

    async def test_untrusted_status_projection_cannot_export_extra_keys_or_coercible_values(
        self,
    ) -> None:
        class ForgedSchema(str):
            def __ne__(self, other: object) -> bool:
                return False

        with self.host():
            handler = self.register()
            for value in (
                dict(WIRE, python_executable="untrusted_operator_value"),
                dict(WIRE, visual="untrusted_operator_value"),
                dict(WIRE, reason="untrusted_operator_value"),
                dict(WIRE, visual=False),
                None,
                dict(WIRE, schema=ForgedSchema("untrusted_operator_value")),
            ):
                with patch.object(self.route, "read_perception_host_status", return_value=value):
                    response = await handler(self.request([]))
                self.assertEqual(response.status, 500, "untrusted_status_projection_was_published")
                self.assertNotIn("untrusted_operator_value", json.dumps(response.body))


if __name__ == "__main__":
    unittest.main()
