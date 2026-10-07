"""M15-04 optional-route, privacy, and publication boundary checks."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

import comfyui_h3_context.adapters.comfyui_sidebar_workspace as adapter
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import MAX_SIDEBAR_ACTION_BYTES
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "comfyui_h3_context" / "adapters" / "comfyui_sidebar_workspace.py"
FRONTEND = ROOT / "frontend" / "src"


class _Routes(list[SimpleNamespace]):
    def post(self, path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method="POST", path=path, handler=handler))
            return handler

        return decorate


class SidebarWorkspaceStaticTests(unittest.TestCase):
    def test_optional_host_imports_are_lazy_and_registration_is_idempotent(self) -> None:
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"), filename=str(ADAPTER))
        top_imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top_imports.add(node.module.split(".", 1)[0])
        self.assertTrue({"aiohttp", "server"}.isdisjoint(top_imports))

        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(adapter.ensure_sidebar_route_registered())
                self.assertTrue(adapter.ensure_sidebar_route_registered())
                self.assertEqual(len(routes), 1)
                self.assertEqual(routes[0].path, "/h3-context/v1/sidebar/action")

        unrelated_server = host_prompt_server_module(object(), available=False)
        with patch.dict(sys.modules, {"server": unrelated_server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertFalse(adapter.ensure_sidebar_route_registered())

    def test_foreign_route_collision_fails_closed_without_claiming_ownership(self) -> None:
        def foreign(_request: object) -> None:
            return None

        routes = _Routes(
            [
                SimpleNamespace(
                    method="POST",
                    path="/h3-context/v1/sidebar/action",
                    handler=foreign,
                )
            ]
        )
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: value
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertFalse(adapter.ensure_sidebar_route_registered())
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].handler, foreign)

    def test_frontend_has_no_html_injection_unsafe_storage_or_reference_runtime(self) -> None:
        sources = {
            path: path.read_text(encoding="utf-8") for path in FRONTEND.rglob("*") if path.is_file()
        }
        source = "\n".join(sources.values())
        # M23-28: the shell is composed from owning modules. Browser-local storage
        # stays with exactly these owners; the composition root touches none of it.
        journal_owner = FRONTEND / "lifecycle" / "extensionRegistration.tsx"
        reattach_owner = FRONTEND / "host" / "managedSequenceClient.ts"
        local_storage_owners = (journal_owner, reattach_owner)
        production_session_owner = FRONTEND / "host" / "productionSession.ts"
        provider_session_owner = FRONTEND / "host" / "providerSession.ts"
        destination_storage_owner = FRONTEND / "host" / "productionDestination.ts"
        storage_owners = (
            production_session_owner,
            provider_session_owner,
            destination_storage_owner,
        )
        journal_source = sources[journal_owner]
        reattach_source = sources[reattach_owner]
        storage_source = "\n".join(sources[path] for path in storage_owners)
        non_local_storage_source = "\n".join(
            text for path, text in sources.items() if path not in local_storage_owners
        )
        non_storage_source = "\n".join(
            text for path, text in sources.items() if path not in storage_owners
        )
        for forbidden in (
            "dangerouslySetInnerHTML",
            ".innerHTML",
            "reference/ui/",
            "h3-context-sidebar-v0",
            "window.fetch(",
        ):
            self.assertNotIn(forbidden, source)
        # IMPORTANT: only the bounded journal and managed reattach host adapter may receive
        # browser-local storage; admitting another owner can leak or duplicate queue authority.
        self.assertNotIn("localStorage", non_local_storage_source)
        self.assertEqual(journal_source.count("globalThis.localStorage"), 1)
        self.assertNotIn("localStorage.getItem(", journal_source)
        self.assertNotIn("localStorage.setItem(", journal_source)
        self.assertNotIn("localStorage.removeItem(", journal_source)
        self.assertEqual(reattach_source.count("globalThis.localStorage.getItem("), 1)
        self.assertEqual(reattach_source.count("globalThis.localStorage.setItem("), 1)
        self.assertEqual(reattach_source.count("globalThis.localStorage.removeItem("), 1)
        self.assertEqual(reattach_source.count("globalThis.localStorage"), 3)
        # IMPORTANT: only opaque session authorities and the bounded workflow destination owner may
        # enter session storage; another owner can leak handles or merge unrelated canvas projects.
        self.assertNotIn("sessionStorage", non_storage_source)
        self.assertEqual(storage_source.count("window.sessionStorage.getItem("), 2)
        self.assertEqual(storage_source.count("window.sessionStorage.setItem("), 2)
        self.assertEqual(storage_source.count("window.sessionStorage.removeItem("), 2)
        self.assertEqual(storage_source.count("window.sessionStorage"), 6)
        production_source = sources[production_session_owner]
        provider_source = sources[provider_session_owner]
        destination_source = sources[destination_storage_owner]
        self.assertIn("PRODUCTION_SESSION_HANDLE_KEY", production_source)
        self.assertIn('"h3.context.production.workspace_handle.v1"', production_source)
        self.assertNotIn("PROVIDER_SESSION_HANDLE_KEY", production_source)
        self.assertIn("PROVIDER_SESSION_HANDLE_KEY", provider_source)
        self.assertIn('"h3.context.provider.session_handle.v1"', provider_source)
        self.assertNotIn("PRODUCTION_SESSION_HANDLE_KEY", provider_source)
        self.assertEqual(destination_source.count("globalThis.window?.sessionStorage"), 1)
        self.assertIn("PRODUCTION_DESTINATION_STORAGE_KEY", destination_source)
        self.assertIn('"h3.context.production.destination.v1"', destination_source)
        self.assertIn("/h3-context/v1/sidebar/action", source)


class _OwnedOrigin:
    """The `getall` surface the shared route seam reads the origin through."""

    def getall(self, name: str, default: list[str]) -> list[str]:
        if name == "Host":
            return [LOOPBACK_HOST]
        return ["http://127.0.0.1:8188"] if name == "Origin" else default


class SidebarWorkspaceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_assisted_route_rejects_origin_and_session_before_reading_body(self) -> None:
        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            self.assertTrue(adapter.ensure_assisted_sidebar_route_registered())
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].path, "/h3-context/v1/sidebar/assisted")

        class Content:
            reads = 0

            async def read(self, _limit: int) -> bytes:
                self.reads += 1
                raise AssertionError("authority rejection read a private body")

        class Headers:
            def __init__(self, values: dict[str, list[str]]) -> None:
                self.values = values

            def getall(self, name: str, default: list[str]) -> list[str]:
                return self.values.get(name, [LOOPBACK_HOST] if name == "Host" else default)

        content = Content()
        rejected_origin = SimpleNamespace(
            content_type="application/json",
            content_length=1,
            content=content,
            headers=Headers({}),
            transport=ListenerTransport(),
        )
        self.assertEqual(
            await routes[0].handler(rejected_origin),
            (403, {"error": "origin_rejected"}),
        )
        rejected_session = SimpleNamespace(
            content_type="application/json",
            content_length=1,
            content=content,
            headers=Headers({"Origin": ["http://127.0.0.1:8188"]}),
            transport=ListenerTransport(),
        )
        self.assertEqual(
            await routes[0].handler(rejected_session),
            (400, {"error": "session_rejected"}),
        )
        self.assertEqual(content.reads, 0)

    async def test_route_rejects_declared_oversize_before_reading_body(self) -> None:
        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(adapter.ensure_sidebar_route_registered())
        request = SimpleNamespace(
            content_type="application/json",
            content_length=MAX_SIDEBAR_ACTION_BYTES + 1,
            headers=_OwnedOrigin(),
            transport=ListenerTransport(),
        )
        status, value = await routes[0].handler(request)
        self.assertEqual(status, 413)
        self.assertEqual(value, {"error": "request_too_large"})

    async def test_route_bounds_chunked_body_before_decoding(self) -> None:
        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(adapter.ensure_sidebar_route_registered())

        class Content:
            async def read(self, limit: int) -> bytes:
                self.limit = limit
                return b"x" * limit

        content = Content()
        request = SimpleNamespace(
            content_type="application/json",
            content_length=None,
            content=content,
            headers=_OwnedOrigin(),
            transport=ListenerTransport(),
        )
        status, value = await routes[0].handler(request)
        self.assertEqual(content.limit, MAX_SIDEBAR_ACTION_BYTES + 1)
        self.assertEqual(status, 413)
        self.assertEqual(value, {"error": "request_too_large"})

    async def test_route_consumes_a_valid_segmented_body_to_eof(self) -> None:
        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(adapter.ensure_sidebar_route_registered())
        body = (
            b'{"schema":"h3.context.sidebar.action.v2",'
            b'"workspace_id":"ws_0123456789abcdefghijklmnopqrstuv",'
            b'"expected_revision":0,"expected_report_fingerprint":"sha256:'
            + b"a" * 64
            + b'","action":"validate","payload":{}}'
        )

        class Content:
            def __init__(self) -> None:
                self.chunks = [body[:20], body[20:], b""]

            async def read(self, _limit: int) -> bytes:
                return self.chunks.pop(0)

        request = SimpleNamespace(
            content_type="application/json",
            content_length=None,
            content=Content(),
            headers=_OwnedOrigin(),
            transport=ListenerTransport(),
        )
        with patch.object(adapter, "dispatch_sidebar_action", return_value={"ok": True}):
            status, value = await routes[0].handler(request)
        self.assertEqual((status, value), (200, {"ok": True}))

    async def test_the_action_route_requires_one_exact_supplied_origin(self) -> None:
        routes = _Routes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = SimpleNamespace(
            json_response=lambda value, status=200, dumps=None: (status, value)
        )
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            with patch.object(adapter, "_ROUTE_REGISTERED", False):
                self.assertTrue(adapter.ensure_sidebar_route_registered())
        body = json.dumps(
            {
                "schema": "h3.context.sidebar.action.v2",
                "workspace_id": "ws_0123456789abcdefghijklmnopqrstuv",
                "expected_revision": 0,
                "expected_report_fingerprint": "sha256:" + "a" * 64,
                "action": "proposal_read",
                "payload": {
                    "review_id": "review_" + "r" * 32,
                    "expected_transaction_fingerprint": "sha256:" + "b" * 64,
                    "expected_workspace_fingerprint": "sha256:" + "c" * 64,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

        class Content:
            def __init__(self) -> None:
                self.done = False

            async def read(self, _limit: int) -> bytes:
                if self.done:
                    return b""
                self.done = True
                return body

        class Headers:
            def __init__(self, origins: list[str]) -> None:
                self.origins = origins

            def getall(self, name: str, default: list[str]) -> list[str]:
                if name == "Host":
                    return [LOOPBACK_HOST]
                return self.origins if name == "Origin" else default

        rejected: tuple[list[str], ...] = (
            [],
            ["http://localhost:8188"],
            ["http://[::1]:8188"],
            ["http://127.0.0.1:8189"],
            ["http://127.0.0.1:8188/path"],
            ["http://127.0.0.1:8188", "http://127.0.0.1:8188"],
            ["http://127.0.0.1:8188,http://foreign.invalid"],
        )
        for origins in rejected:
            request = SimpleNamespace(
                content_type="application/json",
                content_length=len(body),
                content=Content(),
                headers=Headers(origins),
                transport=ListenerTransport(),
            )
            with patch.object(adapter, "dispatch_sidebar_action") as dispatch:
                status, value = await routes[0].handler(request)
            self.assertEqual((status, value), (403, {"error": "origin_rejected"}))
            dispatch.assert_not_called()

        request = SimpleNamespace(
            content_type="application/json",
            content_length=len(body),
            content=Content(),
            headers=Headers(["http://127.0.0.1:8188"]),
            transport=ListenerTransport(),
        )
        with patch.object(adapter, "dispatch_sidebar_action", return_value={"ok": True}):
            status, value = await routes[0].handler(request)
        self.assertEqual((status, value), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
