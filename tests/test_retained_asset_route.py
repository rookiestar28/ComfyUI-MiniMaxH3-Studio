"""Owned retained edges enforce admission, bounded IO and actual worker lifetime."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_m23_47_route_seam import _Headers, _ReadableContent, _Routes, _UnreadableContent, _web
from test_recovery_owner import server_facts
from test_retained_asset_service import dispatch, enable_and_retain, service_fixture

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwnerPort
from comfyui_h3_context.adapters.retained_asset_service import RetainedAssetService
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

MODULE = "comfyui_h3_context.adapters.comfyui_retained_assets"


def wire(response: Any) -> dict[str, Any]:
    value: object = (
        json.loads(response.body)
        if isinstance(response.body, (bytes, bytearray))
        else response.body
    )
    assert isinstance(value, dict)
    return value


class RetainedAssetRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).absolute()
        self.facts = server_facts()
        self.qualifications: list[Path] = []

        def filesystem(path: Path) -> bool:
            self.qualifications.append(path)
            return True

        port = RecoveryOwnerPort(
            host=SimpleNamespace(
                private_root=lambda: self.root / "private",
                served_roots=lambda: (
                    self.root / "input",
                    self.root / "output",
                    self.root / "temp",
                ),
            ),
            host_facts=lambda: self.facts,
            public_origins=lambda: False,
            filesystem=filesystem,
        )
        self.service = RetainedAssetService(
            owner_port=port, production=lambda: self.fail("no generation owner on status")
        )
        self.addCleanup(self.service.close)
        self.routes = _Routes()

    def register(self) -> tuple[ModuleType, dict[str, Any]]:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE), "missing bounded retained media edge"
        )
        module = importlib.import_module(MODULE)
        self.assertTrue(hasattr(composition_root, "RETAINED_ASSETS"), "missing lazy retained owner")
        substitution = composition_root.substituted(composition_root.RETAINED_ASSETS, self.service)
        substitution.__enter__()
        self.addCleanup(substitution.__exit__, None, None, None)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        patcher = patch.dict(
            sys.modules, {"server": host_prompt_server_module(self.routes), "aiohttp": aiohttp}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(module.ensure_retained_assets_routes_registered())
        self.assertEqual(len(self.routes), 2)
        return module, {row.path: row.handler for row in self.routes}

    def request(
        self,
        action: dict[str, Any] | None = None,
        *,
        origin: list[str] | None = None,
        peer: str = "127.0.0.1",
        unreadable: bool = False,
        length: int | None = None,
        closing: bool = False,
    ) -> SimpleNamespace:
        payload = json.dumps(action or {"intent": "status"}).encode()
        transport = SimpleNamespace(
            get_extra_info=lambda name: {
                "sockname": ("127.0.0.1", 8188),
                "peername": (peer, 5555),
            }.get(name),
            is_closing=lambda: closing,
        )
        return SimpleNamespace(
            content_type="application/json",
            content_length=len(payload) if length is None else length,
            content=_UnreadableContent() if unreadable else _ReadableContent(payload),
            headers=_Headers(["http://127.0.0.1:8188"] if origin is None else origin),
            transport=transport,
            query_string="",
        )

    async def test_origin_peer_and_host_refuse_before_body_storage_or_owner_creation(self) -> None:
        module, handlers = self.register()
        for path in (module.RETAINED_ASSETS_ROUTE, module.RETAINED_PREVIEW_ROUTE):
            rejected_options: tuple[dict[str, Any], ...] = (
                {"origin": ["http://foreign.invalid"]},
                {"peer": "192.0.2.1"},
            )
            for options in rejected_options:
                response = await handlers[path](self.request(unreadable=True, **options))
                self.assertEqual(response.status, 403)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.qualifications, [])
        self.facts = server_facts(multi_user=True)
        response = await handlers[module.RETAINED_ASSETS_ROUTE](self.request(unreadable=True))
        self.assertEqual(response.status, 403)
        self.assertEqual(wire(response)["error"], "host_unqualified")
        self.assertFalse((self.root / "private").exists())

    async def test_default_off_enable_and_stale_cas_have_no_store_safe_wires(self) -> None:
        module, handlers = self.register()
        handler = handlers[module.RETAINED_ASSETS_ROUTE]
        initial = await handler(self.request())
        self.assertEqual(initial.status, 200)
        self.assertFalse(wire(initial)["projection"]["enabled"])
        self.assertFalse((self.root / "private").exists())
        enabled = await handler(
            self.request({"intent": "set_enabled", "enabled": True, "expected_revision": 0})
        )
        self.assertEqual(enabled.status, 200)
        stale = await handler(self.request({"intent": "clear", "expected_revision": 0}))
        self.assertEqual(stale.status, 409)
        self.assertEqual(wire(stale)["error"], "revision_conflict")
        self.assertEqual(stale.headers["Cache-Control"], "no-store")
        self.assertNotIn(str(self.root), json.dumps(wire(enabled)))

    async def test_declared_streaming_range_query_and_decode_bounds_fail_closed(self) -> None:
        module, handlers = self.register()
        for path in (module.RETAINED_ASSETS_ROUTE, module.RETAINED_PREVIEW_ROUTE):
            handler = handlers[path]
            response = await handler(self.request(length=8193, unreadable=True))
            self.assertEqual(response.status, 413)
            request = self.request()
            request.content_length = None
            request.content = _ReadableContent(b" " * 8193)
            self.assertEqual((await handler(request)).status, 413)
            request = self.request(unreadable=True)
            request.query_string = "path=private"
            self.assertEqual((await handler(request)).status, 400)
            response = await handler(self.request({"intent": "status", "path": "sensitive"}))
            self.assertEqual(response.status, 400)
            self.assertNotIn("sensitive", json.dumps(wire(response)))
        self.assertFalse((self.root / "private").exists())

    async def test_closed_transport_and_slow_body_never_start_storage(self) -> None:
        module, handlers = self.register()
        handler = handlers[module.RETAINED_ASSETS_ROUTE]
        response = await handler(self.request(closing=True, unreadable=True))
        self.assertEqual(response.status, 499)

        class SlowBody:
            async def read(self, _size: int) -> bytes:
                await asyncio.sleep(1)
                return b""

        request = self.request()
        request.content = SlowBody()
        with patch.object(module, "WORK_DEADLINE_SECONDS", 0.02):
            response = await handler(request)
        self.assertEqual(response.status, 504)
        self.assertFalse((self.root / "private").exists())

    async def test_cancelled_coroutine_keeps_admission_until_worker_really_exits(self) -> None:
        module, handlers = self.register()
        handler = handlers[module.RETAINED_ASSETS_ROUTE]
        entered, exit_worker, finalized = threading.Event(), threading.Event(), threading.Event()
        dispatch = self.service.dispatch

        def slow(action: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            entered.set()
            if not exit_worker.wait(2):
                raise RuntimeError("worker test timeout")
            try:
                return dispatch(action, **kwargs)
            finally:
                finalized.set()

        with patch.object(self.service, "dispatch", side_effect=slow):
            task = asyncio.create_task(handler(self.request()))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            busy = await handler(self.request())
            self.assertEqual(busy.status, 429)
            self.assertFalse(finalized.is_set())
            exit_worker.set()
            for _ in range(100):
                if not module.retained_assets_busy():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(finalized.is_set())
        self.assertEqual((await handler(self.request())).status, 200)

    async def test_unknown_preview_never_requests_runtime_and_is_no_store(self) -> None:
        module, handlers = self.register()
        response = await handlers[module.RETAINED_PREVIEW_ROUTE](
            self.request({"use_handle": "retained_" + "a" * 32})
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(wire(response)["error"], "lease_invalid")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_owned_release_can_finish_while_another_worker_is_in_flight(self) -> None:
        module, handlers = self.register()
        handler = handlers[module.RETAINED_ASSETS_ROUTE]
        entered, exit_worker = threading.Event(), threading.Event()
        dispatch = self.service.dispatch

        def slow(action: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if action["intent"] == "status":
                entered.set()
                if not exit_worker.wait(2):
                    raise RuntimeError("worker test timeout")
            return dispatch(action, **kwargs)

        with patch.object(self.service, "dispatch", side_effect=slow):
            task = asyncio.create_task(handler(self.request()))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                released = await handler(
                    self.request({"intent": "release", "use_handle": "retained_" + "a" * 32})
                )
                self.assertEqual(released.status, 200)
                self.assertTrue(module.retained_assets_busy())
            finally:
                exit_worker.set()
                await task

    async def test_repeated_or_foreign_registration_never_replaces_handlers(self) -> None:
        module, _ = self.register()
        self.assertTrue(module.ensure_retained_assets_routes_registered())
        self.assertEqual(len(self.routes), 2)
        foreign = self.routes[0].handler = lambda _: None
        self.assertFalse(module.ensure_retained_assets_routes_registered())
        self.assertIs(self.routes[0].handler, foreign)

    async def test_release_during_preview_preserves_borrowed_bytes_and_discards_late_result(
        self,
    ) -> None:
        entered, exit_worker = threading.Event(), threading.Event()
        late_body = bytearray(b"late-preview-must-be-cleared")
        with pytest.MonkeyPatch.context() as monkeypatch:
            self.service.close()
            self.service, command, _, adapter, _ = service_fixture(self.root, monkeypatch)
            self.addCleanup(self.service.close)
            _, identifier = enable_and_retain(self.service, command)
            restored = dispatch(
                self.service,
                {"intent": "restore", "asset_id": identifier, "expected_revision": 2},
            )
            handle = restored["restored"]["use_handle"]
            paths: list[Path] = []

            def slow_preview(
                _adapter: QualifiedAVMediaAdapter, **kwargs: Any
            ) -> tuple[bytearray, str]:
                path = kwargs["source_path"]
                paths.append(path)
                self.assertEqual(path.read_bytes(), b"bounded-generated-video-1")
                entered.set()
                if not exit_worker.wait(3):
                    raise RuntimeError("worker test timeout")
                self.assertTrue(path.exists(), "borrowed bytes closed before native work ended")
                return late_body, "none"

            monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", slow_preview)
            module, handlers = self.register()
            handler = handlers[module.RETAINED_ASSETS_ROUTE]
            preview = asyncio.create_task(
                handlers[module.RETAINED_PREVIEW_ROUTE](self.request({"use_handle": handle}))
            )
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                released = await handler(self.request({"intent": "release", "use_handle": handle}))
                self.assertEqual(released.status, 200)
                self.assertEqual(wire(released)["projection"]["protected"], 1)
                self.assertTrue(paths[0].exists())
                self.assertTrue(module.retained_assets_busy())
            finally:
                exit_worker.set()
                response = await preview
            self.assertEqual(response.status, 409)
            self.assertEqual(wire(response)["error"], "source_stale")
            self.assertEqual(late_body, bytearray())
            self.assertFalse(paths[0].exists())
            self.assertFalse(list(adapter._scratch_root.iterdir()))
            current = wire(await handler(self.request()))["projection"]
            self.assertEqual((current["count"], current["protected"]), (1, 0))
            old = await handlers[module.RETAINED_PREVIEW_ROUTE](
                self.request({"use_handle": handle})
            )
            self.assertEqual(old.status, 404)


if __name__ == "__main__":
    unittest.main()
