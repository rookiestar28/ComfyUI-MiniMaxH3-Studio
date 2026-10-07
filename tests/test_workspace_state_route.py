"""The bounded recovery edge never derives an owner from caller-provided fields."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from test_m23_47_route_seam import _Headers, _ReadableContent, _Routes, _UnreadableContent, _web
from test_recovery_owner import server_facts

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwnerPort
from comfyui_h3_context.adapters.workspace_state_service import WorkspaceStateService
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

MODULE = "comfyui_h3_context.adapters.comfyui_workspace_state"


class WorkspaceStateRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).absolute()
        self.qualifications: list[Path] = []
        self.facts = server_facts()
        host = SimpleNamespace(
            private_root=lambda: self.root / "private",
            served_roots=lambda: (self.root / "input", self.root / "output", self.root / "temp"),
        )

        def filesystem(path: Path) -> bool:
            self.qualifications.append(path)
            return True

        port = RecoveryOwnerPort(
            host=host,
            host_facts=lambda: self.facts,
            public_origins=lambda: False,
            filesystem=filesystem,
        )
        self.service = WorkspaceStateService(owner_port=port, collector=lambda: ())
        self.addCleanup(self.service.close)
        self.routes = _Routes()

    def register(self) -> tuple[ModuleType, Callable[..., Awaitable[Any]]]:
        self.assertIsNotNone(importlib.util.find_spec(MODULE), "missing bounded recovery route")
        module = importlib.import_module(MODULE)
        self.assertTrue(
            hasattr(composition_root, "WORKSPACE_STATE"), "missing composed state service"
        )
        component = composition_root.WORKSPACE_STATE
        substitution = composition_root.substituted(component, self.service)
        substitution.__enter__()
        self.addCleanup(substitution.__exit__, None, None, None)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        server = host_prompt_server_module(self.routes)
        patcher = patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(module.ensure_workspace_state_route_registered())
        self.assertEqual(len(self.routes), 1)
        return module, self.routes[0].handler

    def request(
        self,
        action: dict[str, object] | None = None,
        *,
        origins: list[str] | None = None,
        peer: str = "127.0.0.1",
        unreadable: bool = False,
        length: int | None = None,
    ) -> SimpleNamespace:
        payload = json.dumps(action or {"intent": "status"}).encode()
        transport = SimpleNamespace(
            get_extra_info=lambda name: {
                "sockname": ("127.0.0.1", 8188),
                "peername": (peer, 5555),
            }.get(name)
        )
        return SimpleNamespace(
            content_type="application/json",
            content_length=len(payload) if length is None else length,
            content=_UnreadableContent() if unreadable else _ReadableContent(payload),
            headers=_Headers(["http://127.0.0.1:8188"] if origins is None else origins),
            transport=transport,
        )

    async def test_foreign_origin_and_nonlocal_peer_refuse_before_body_or_qualification(
        self,
    ) -> None:
        _, handler = self.register()
        for options in ({"origins": ["http://foreign.invalid"]}, {"peer": "192.0.2.1"}):
            with self.subTest(options=options):
                response = await handler(self.request(unreadable=True, **options))
                self.assertEqual(response.status, 403)
                self.assertEqual(response.body["projection"]["records"], [])
        self.assertEqual(self.qualifications, [])
        self.assertFalse((self.root / "private").exists())

    async def test_unqualified_mode_refuses_before_body_and_filesystem(self) -> None:
        _, handler = self.register()
        self.facts = server_facts(multi_user=True)
        response = await handler(self.request(unreadable=True))
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"], "host_unqualified")
        self.assertEqual(self.qualifications, [])

    async def test_real_default_off_enable_save_and_stale_cas_are_typed(self) -> None:
        _, handler = self.register()
        initial = await handler(self.request())
        self.assertEqual(initial.status, 200)
        self.assertFalse(initial.body["projection"]["enabled"])
        self.assertFalse((self.root / "private").exists())
        enabled = await handler(
            self.request({"intent": "set_enabled", "enabled": True, "expected_revision": 0})
        )
        self.assertEqual(enabled.status, 200)
        revision = enabled.body["projection"]["revision"]
        saved = await handler(self.request({"intent": "save", "expected_revision": revision}))
        self.assertEqual(saved.status, 200)
        self.assertEqual(saved.body["projection"]["save_state"], "saved")
        stale = await handler(self.request({"intent": "reset", "expected_revision": revision}))
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.body["error"], "revision_conflict")
        self.assertTrue(self.service.dispatch({"intent": "status"})["projection"]["enabled"])

    async def test_body_bounds_and_caller_owner_path_data_are_refused_without_disclosure(
        self,
    ) -> None:
        _, handler = self.register()
        for name in ("owner_id", "path", "data"):
            with self.subTest(name=name):
                response = await handler(self.request({"intent": "status", name: "private-value"}))
                self.assertEqual(response.status, 400)
                self.assertNotIn("private-value", json.dumps(response.body))
        response = await handler(self.request(length=4097, unreadable=True))
        self.assertEqual(response.status, 413)
        self.assertFalse((self.root / "private").exists())

    async def test_repeated_registration_is_owned_and_foreign_route_is_not_replaced(self) -> None:
        module, _ = self.register()
        self.assertTrue(module.ensure_workspace_state_route_registered())
        self.assertEqual(len(self.routes), 1)
        foreign = self.routes[0].handler = lambda _request: None
        self.assertFalse(module.ensure_workspace_state_route_registered())
        self.assertIs(self.routes[0].handler, foreign)


if __name__ == "__main__":
    unittest.main()
