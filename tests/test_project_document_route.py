"""Project routes have independent bounds, finite commands and no imported grants."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from test_m23_47_route_seam import _Headers, _ReadableContent, _Routes, _UnreadableContent, _web
from test_project_document import document_wire

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService
from scripts.hc_09_host_seam_test_double import host_prompt_server_module


class ProjectDocumentRouteTests(unittest.IsolatedAsyncioTestCase):
    def register(self) -> tuple[ModuleType, ProjectDocumentService, Any]:
        name = "comfyui_h3_context.adapters.comfyui_project_document"
        self.assertIsNotNone(
            importlib.util.find_spec(name), "bounded project document route is required"
        )
        module = importlib.import_module(name)
        service = ProjectDocumentService(
            ProductionWorkspaceRegistry(seed_claim=Mock()), AuthoringWorkspaceRegistry()
        )
        substitution = composition_root.substituted(composition_root.PROJECT_DOCUMENT, service)
        substitution.__enter__()
        self.addCleanup(substitution.__exit__, None, None, None)
        self.routes = _Routes()
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        patcher = patch.dict(
            sys.modules, {"server": host_prompt_server_module(self.routes), "aiohttp": aiohttp}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(module.ensure_project_document_route_registered())
        self.assertTrue(module.ensure_project_document_route_registered())
        self.assertEqual(len(self.routes), 1)
        return module, service, self.routes[0].handler

    def request(
        self,
        value: object,
        *,
        origin: str = "http://127.0.0.1:8188",
        unreadable: bool = False,
        length: int | None = None,
        closing: bool = False,
    ) -> SimpleNamespace:
        payload = json.dumps(value).encode()
        transport = SimpleNamespace(
            get_extra_info=lambda name: {
                "sockname": ("127.0.0.1", 8188),
                "peername": ("127.0.0.1", 55),
            }.get(name),
            is_closing=lambda: closing,
        )
        return SimpleNamespace(
            content_type="application/json",
            headers=_Headers([origin]),
            content_length=len(payload) if length is None else length,
            content=_UnreadableContent() if unreadable else _ReadableContent(payload),
            transport=transport,
            host="127.0.0.1:8188",
            scheme="http",
            query={},
            method="POST",
        )

    async def test_actual_open_export_pair_and_no_store(self) -> None:
        _module, _service, handler = self.register()
        response = await handler(self.request({"intent": "open", "document": document_wire()}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.body
        request = {
            "intent": "export",
            "owner": body["owner"],
            "planning": document_wire()["planning"],
            "title": document_wire()["title"],
        }
        saved = await handler(self.request(request))
        self.assertEqual(saved.status, 200)
        self.assertEqual(saved.body["document"], document_wire())

    async def test_origin_size_and_unknown_command_refuse_before_staging(self) -> None:
        module, service, handler = self.register()
        foreign = await handler(self.request({}, origin="https://foreign.invalid", unreadable=True))
        self.assertEqual(foreign.status, 403)
        self.assertEqual(foreign.headers.get("Cache-Control"), "no-store")
        self.assertEqual(
            (
                await handler(
                    self.request({}, unreadable=True, length=module.MAX_PROJECT_REQUEST_BYTES + 1)
                )
            ).status,
            413,
        )
        self.assertEqual(
            (await handler(self.request({"intent": "execute", "path": "forbidden"}))).status, 400
        )
        self.assertFalse(service.authoring._entries)
        self.assertFalse(service.production._editable_projects)

    async def test_closed_transport_cannot_publish_new_pair(self) -> None:
        _module, service, handler = self.register()
        response = await handler(
            self.request({"intent": "open", "document": document_wire()}, closing=True)
        )
        self.assertNotEqual(response.status, 200)
        self.assertFalse(service.authoring._entries)
        self.assertFalse(service.production._editable_projects)


if __name__ == "__main__":
    unittest.main()
