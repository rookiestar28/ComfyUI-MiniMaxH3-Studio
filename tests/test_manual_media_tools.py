"""Manual media-tool setup contract: no executable acquisition or implicit retry."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from deployment_request_doubles import LOOPBACK_HOST, ListenerTransport

from comfyui_h3_context.adapters import comfyui_media_runtime_setup as setup
from scripts.hc_09_host_seam_test_double import host_prompt_server_module


def test_manual_wire_retires_jobs_and_install_actions() -> None:
    assert setup.STATUS_SCHEMA == "h3.context.media_runtime_status.v4"
    assert setup.REQUEST_SCHEMA == "h3.context.media_runtime_setup_request.v2"
    assert set(setup.ACTIONS) == {
        "rescan",
        "use_local_directory",
        "restore_auto",
        "reclaim_parked_runtime",
    }
    assert not hasattr(setup, "JOB_ROUTE")
    assert not hasattr(setup.MediaRuntimeSetupService, "start_install")


@pytest.mark.parametrize("action", ["install_supported", "cancel_setup"])
def test_retired_actions_are_refused_before_any_work(action: str) -> None:
    def forbidden() -> None:
        pytest.fail("retired action reached implementation")

    service = SimpleNamespace(start_install=forbidden, cancel=forbidden)
    with pytest.raises(setup.MediaRuntimeSetupError) as error:
        setup.dispatch_setup_request(
            cast(setup.MediaRuntimeSetupService, service),
            {"schema": setup.REQUEST_SCHEMA, "action": action},
        )
    assert (error.value.code, error.value.status) == ("invalid_request", 400)


class _Routes(list[SimpleNamespace]):
    def _decorator(self, method: str, path: str) -> Callable[[Callable[..., Any]], Any]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.append(SimpleNamespace(method=method, path=path, handler=handler))
            return handler

        return decorate

    def get(self, path: str) -> Callable[[Callable[..., Any]], Any]:
        return self._decorator("GET", path)

    def post(self, path: str) -> Callable[[Callable[..., Any]], Any]:
        return self._decorator("POST", path)


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    def getall(self, name: str, default: list[str]) -> list[str]:
        return self._values.get(name, [LOOPBACK_HOST] if name == "Host" else default)


class _Content:
    def __init__(self, body: bytes) -> None:
        self._chunks = [body, b""]
        self.reads = 0

    async def read(self, _limit: int) -> bytes:
        self.reads += 1
        return self._chunks.pop(0)


def http_request(
    body: bytes = b"",
    *,
    origin: list[str] | None = None,
    site: list[str] | None = None,
    content_type: str = "application/json",
    job_id: str | None = None,
) -> SimpleNamespace:
    headers = {"Origin": ["http://127.0.0.1:8188"] if origin is None else origin}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    return SimpleNamespace(
        content_type=content_type,
        content_length=len(body),
        content=_Content(body),
        headers=_Headers(headers),
        transport=ListenerTransport(),
        match_info={"job_id": job_id} if job_id is not None else {},
    )


def registered() -> dict[tuple[str, str], Callable[..., Any]]:
    routes = _Routes()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(
        json_response=lambda value, status=200, headers=None: (status, value),
        Response=lambda status=200, body=None, headers=None: (status, body),
    )
    with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
        with patch.object(routes_module, "_ROUTE_REGISTERED", False):
            assert ensure_media_runtime_setup_route_registered()
            assert ensure_media_runtime_setup_route_registered()
    assert len(routes) == 2
    return {(route.method, route.path): route.handler for route in routes}


def test_package_import_builds_no_setup_service_and_opens_no_connection() -> None:
    script = (
        "import socket, subprocess\n"
        "class Refuse(subprocess.Popen):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise SystemExit('process spawned during import')\n"
        "subprocess.Popen = Refuse\n"
        "def refuse(*a, **k):\n"
        "    raise SystemExit('network used during import')\n"
        "socket.create_connection = refuse\n"
        "socket.getaddrinfo = refuse\n"
        "import comfyui_h3_context\n"
        "from comfyui_h3_context.adapters import composition_root as root\n"
        "import comfyui_h3_context.adapters.comfyui_media_runtime_setup\n"
        "assert root.installed(root.MEDIA_RUNTIME_SETUP) is None\n"
        "assert root.installed(root.MEDIA_RUNTIME_RESOLVER) is None\n"
        "print('inert')\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("H3_CONTEXT_")
    }
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(routes_module.__file__).resolve().parents[2],
        env=environment,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().endswith("inert")


# Retained HTTP edge and CAS coverage formerly mixed into installer tests.
routes_module = setup
STATUS_SCHEMA = setup.STATUS_SCHEMA
STATUS_ROUTE = setup.STATUS_ROUTE
SETUP_ROUTE = setup.SETUP_ROUTE
ensure_media_runtime_setup_route_registered = setup.ensure_media_runtime_setup_route_registered


@pytest.mark.parametrize(
    "raw",
    [b"{", b"[]", b"\xff", b'{"action":"rescan","action":"restore_auto"}', b'{"revision":NaN}'],
)
def test_decoder_refuses_ambiguous_or_non_json_bodies(raw: bytes) -> None:
    with pytest.raises(setup.MediaRuntimeSetupError):
        setup.decode_setup_request(raw)


@pytest.mark.parametrize("revision", [True, -1, 1.5, "0", 2**53])
def test_revision_is_a_bounded_integer_before_any_config_write(revision: object) -> None:
    with pytest.raises(setup.MediaRuntimeSetupError):
        setup.dispatch_setup_request(
            cast(
                setup.MediaRuntimeSetupService,
                SimpleNamespace(
                    restore_auto=lambda _revision: pytest.fail("invalid revision reached write")
                ),
            ),
            {
                "schema": setup.REQUEST_SCHEMA,
                "action": "restore_auto",
                "expected_revision": revision,
            },
        )


@pytest.mark.parametrize("path,method", [(STATUS_ROUTE, "GET"), (SETUP_ROUTE, "POST")])
def test_foreign_route_leaves_both_manual_routes_unregistered(path: str, method: str) -> None:
    routes = _Routes([SimpleNamespace(method=method, path=path, handler=lambda _request: None)])
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = SimpleNamespace(json_response=lambda value, status=200: value)
    with patch.dict(sys.modules, {"server": host_prompt_server_module(routes), "aiohttp": aiohttp}):
        assert not setup.ensure_media_runtime_setup_route_registered()
    assert len(routes) == 1


def test_only_status_and_manual_post_register_idempotently() -> None:
    assert set(registered()) == {("GET", STATUS_ROUTE), ("POST", SETUP_ROUTE)}


def test_http_rejects_origin_and_retired_actions_before_service_work() -> None:
    handlers = registered()
    service = SimpleNamespace(
        status=lambda: {"schema": STATUS_SCHEMA},
        rescan=lambda: (_ for _ in ()).throw(OSError("private path")),
    )

    def body(action: str) -> bytes:
        return json.dumps({"schema": setup.REQUEST_SCHEMA, "action": action}).encode()

    async def exercise() -> None:
        read = handlers[("GET", STATUS_ROUTE)]
        write = handlers[("POST", SETUP_ROUTE)]
        assert await read(http_request(origin=[])) == (200, {"schema": STATUS_SCHEMA})
        assert await read(http_request(origin=[], site=["cross-site"])) == (
            403,
            {"error": "origin_rejected"},
        )
        foreign = http_request(body("rescan"), origin=["https://foreign.example"])
        assert await write(foreign) == (403, {"error": "origin_rejected"})
        assert foreign.content.reads == 0
        assert await write(http_request(b"{}", content_type="text/plain")) == (
            415,
            {"error": "media_type_rejected"},
        )
        assert await write(http_request(b"x" * (setup.MAX_SETUP_REQUEST_BYTES + 1))) == (
            413,
            {"error": "request_too_large"},
        )
        assert await write(http_request(b"{")) == (400, {"error": "invalid_request"})
        for retired in ("install_supported", "cancel_setup"):
            assert await write(http_request(body(retired))) == (400, {"error": "invalid_request"})
        assert await write(http_request(body("rescan"))) == (500, {"error": "internal_failure"})

    with patch.object(setup, "media_runtime_setup", lambda: service):
        asyncio.run(exercise())


# IMPORTANT: a local selection must be a drive-absolute Windows directory
# (`lexical_local_directory`), so a POSIX temporary path is refused before this flow starts. Only
# the Linux Hosted backend runs this file off Windows; keep the marker with the real resolver.
@pytest.mark.skipif(os.name != "nt", reason="Windows drive-absolute local directory")
def test_offline_real_resolver_selection_refusal_path_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_m25_30_media_runtime_resolution import inputs, make_pair, resolver

    def no_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("manual setup attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    pair = make_pair(tmp_path / "supported")
    current = [inputs()]
    runtime, _, _ = resolver(tmp_path, current[0], current=current)
    service = setup.MediaRuntimeSetupService(resolver=runtime)
    service.use_local_directory(str(pair), 0)
    assert runtime.resolve().source_kind.value == "local_selection"
    before = runtime.private_layout().config_file.read_bytes()
    for directory in (
        tmp_path / "missing",
        make_pair(tmp_path / "tampered", ffmpeg=b"wrong build"),
    ):
        with pytest.raises(setup.MediaRuntimeSetupError):
            service.use_local_directory(str(directory), 1)
        assert runtime.private_layout().config_file.read_bytes() == before
    service.restore_auto(1)
    config = runtime.read_config()
    assert config is not None and config.selection.value == "auto"
    current.append(inputs(path=str(pair)))
    service.rescan()
    assert runtime.resolve().source_kind.value == "system_path"
