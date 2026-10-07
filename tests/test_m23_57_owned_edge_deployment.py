"""M23-57: every registered owned route, under every supported deployment, through its real handler.

The pure rule is covered in `test_m23_57_request_target.py`. This file drives the registered
handlers -- the seam's JSON routes, both preview edges, both lease routes and the authoring output
routes -- with request doubles that carry what a real aiohttp request carries (`Host`, `Origin`,
`Sec-Fetch-Site` and a transport exposing the accepting socket), and observes admission through the
seam's single diagnostic hook, so a refusal is always attributed to its reason.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from deployment_request_doubles import ListenerTransport
from test_m23_47_route_seam import (
    ABSENT_ORIGIN_ROUTES,
    _NotedRefusals,
    _ReadableContent,
    _register_everything,
)
from test_recovery_owner import server_facts

from comfyui_h3_context.adapters import comfyui_route_seam as seam
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.editor_recovery_service import EditorRecoveryService
from comfyui_h3_context.adapters.project_document_service import ProjectDocumentService
from comfyui_h3_context.adapters.recovery_owner import RecoveryOwnerPort
from comfyui_h3_context.adapters.retained_asset_service import RetainedAssetService
from comfyui_h3_context.adapters.workspace_state_service import WorkspaceStateService
from comfyui_h3_context.core.request_target import (
    PUBLIC_ORIGINS_VARIABLE,
    AdmissionReason,
    parse_public_origins,
)

PROXY_ORIGIN = "https://h3-proxy.localhost:8443"

#: One row per supported deployment: the socket the host accepted the connection on, whether it is
#: native TLS, the `Host` the browser sent and the page's own origin.
VENUES: dict[str, tuple[tuple[Any, ...], bool, str, str]] = {
    "P01 IPv4 loopback": (("127.0.0.1", 8188), False, "127.0.0.1:8188", "http://127.0.0.1:8188"),
    "P02 localhost": (("127.0.0.1", 8188), False, "localhost:8188", "http://localhost:8188"),
    "P03 another port": (("127.0.0.1", 8000), False, "127.0.0.1:8000", "http://127.0.0.1:8000"),
    "P03 localhost, another port": (
        ("::1", 8000, 0, 0),
        False,
        "localhost:8000",
        "http://localhost:8000",
    ),
    "P04 IPv6 loopback": (("::1", 8000, 0, 0), False, "[::1]:8000", "http://[::1]:8000"),
    "P05 served interface": (
        ("10.20.30.40", 8000),
        False,
        "10.20.30.40:8000",
        "http://10.20.30.40:8000",
    ),
    "P05 wildcard dual-stack": (
        ("::ffff:192.168.10.20", 8188, 0, 0),
        False,
        "192.168.10.20:8188",
        "http://192.168.10.20:8188",
    ),
    "native TLS": (("127.0.0.1", 8443), True, "localhost:8443", "https://localhost:8443"),
    "P06 configured proxy": (
        ("127.0.0.1", 8188),
        False,
        "h3-proxy.localhost:8443",
        PROXY_ORIGIN,
    ),
}

#: Request attributes an edge may read before it has decided admission. Anything else read on a
#: refused request is work done for a caller who was never entitled to it (N06).
ADMISSION_ATTRIBUTES = frozenset({"content_type", "headers", "transport"})


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self._values = values

    def getall(self, name: str, default: list[str]) -> list[str]:
        return list(self._values.get(name, default))


class _Refused:
    """A request that records every attribute a handler touches, and yields no body."""

    def __init__(self, headers: dict[str, list[str]], transport: ListenerTransport) -> None:
        object.__setattr__(self, "touched", [])
        object.__setattr__(self, "content_type", "application/json")
        object.__setattr__(self, "headers", _Headers(headers))
        object.__setattr__(self, "transport", transport)

    def __getattribute__(self, name: str) -> Any:
        if name != "touched" and not name.startswith("__"):
            object.__getattribute__(self, "touched").append(name)
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _headers(
    host: str, origin: str | None, *, extra: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {"Host": [host], "Origin": [] if origin is None else [origin]}
    values["Sec-Fetch-Site"] = ["same-origin"]
    values.update(extra or {})
    return values


def _admissible(
    host: str, origin: str | None, sockname: tuple[Any, ...], *, secure: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        method="POST",
        content_type="application/json",
        content_length=2,
        can_read_body=True,
        content=_ReadableContent(b"{}"),
        headers=_Headers(_headers(host, origin)),
        query_string="",
        query=SimpleNamespace(keys=lambda: [], getall=lambda _name, default: default),
        match_info={},
        transport=ListenerTransport(sockname, secure=secure),
    )


class _Configured:
    """Installs one operator configuration for the process-wide cache, and restores it."""

    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def __enter__(self) -> None:
        self._patchers = [
            patch.object(seam, "_PUBLIC_ORIGINS", parse_public_origins(self._raw)),
        ]
        for patcher in self._patchers:
            patcher.start()

    def __exit__(self, *_exc: object) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()


#: The venue every other venue's answers are compared with: the one deployment the product served
#: before admission became target-relative.
REFERENCE_VENUE = "P01 IPv4 loopback"

PRIVATE_ROUTES = {
    ("POST", "/h3-context/project-recovery"): "command_invalid",
    ("POST", "/h3-context/workspace-state"): "intent_invalid",
    ("POST", "/h3-context/retained-assets"): "intent_invalid",
    ("POST", "/h3-context/retained-assets/preview"): "shape_invalid",
}
LOOPBACK_VENUES = {name: row for name, row in VENUES.items() if not name.startswith(("P05", "P06"))}


def _general_routes() -> list[Any]:
    routes = list(_register_everything())
    assert set(PRIVATE_ROUTES) <= {(route.method, route.path) for route in routes}
    # CRITICAL: private routes additionally require qualified loopback owners, not LAN/proxy
    # admission alone. Cover them below; never hide an arbitrary route's 403 here.
    return [route for route in routes if (route.method, route.path) not in PRIVATE_ROUTES]


class _PeerTransport(ListenerTransport):
    def __init__(self, sockname: object, peer: object, *, secure: bool = False) -> None:
        super().__init__(sockname, secure=secure)
        self._peer = peer

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._peer if name == "peername" else super().get_extra_info(name, default)


class EveryRouteServesEverySupportedDeploymentTests(unittest.IsolatedAsyncioTestCase):
    """Admission is invisible to a route: each venue's own page gets the reference venue's answer.

    CRITICAL: the diagnostic hook alone is not enough. An edge that refuses on its own -- a private
    origin constant or a literal `Host` pin, the two defects this item removed from the preview and
    lease edges -- never reaches `_note_refusal`, so a positive test that only asserted "no refusal
    was noted" stayed green with the lease `Host` pin put back (mutation M3). Each answer is
    therefore compared with the reference venue's answer for the same route, and the reference
    answers are themselves checked not to be refusals.
    """

    async def _reference(
        self, routes: list[Any], *, absent_origin: bool = False
    ) -> dict[tuple[str, str], int | None]:
        sockname, secure, host, origin = VENUES[REFERENCE_VENUE]
        answers: dict[tuple[str, str], int | None] = {}
        for route in routes:
            response = await route.handler(
                _admissible(host, None if absent_origin else origin, sockname, secure=secure)
            )
            answers[(route.method, route.path)] = getattr(response, "status", None)
        self.assertNotIn(403, answers.values())
        self.assertNotIn(None, answers.values())
        return answers

    async def _assert_served(
        self,
        routes: list[Any],
        venues: dict[str, tuple[tuple[Any, ...], bool, str, str]],
        *,
        absent_origin: bool = False,
    ) -> None:
        expected = await self._reference(routes, absent_origin=absent_origin)
        for venue, (sockname, secure, host, origin) in venues.items():
            for route in routes:
                with self.subTest(venue=venue, method=route.method, path=route.path):
                    request = _admissible(
                        host, None if absent_origin else origin, sockname, secure=secure
                    )
                    with _NotedRefusals() as noted:
                        response = await route.handler(request)
                    self.assertEqual(noted.reasons, [])
                    self.assertEqual(
                        getattr(response, "status", None), expected[(route.method, route.path)]
                    )

    async def test_each_deployment_is_admitted_for_its_own_page(self) -> None:
        with _Configured(PROXY_ORIGIN):
            await self._assert_served(_general_routes(), VENUES)

    async def test_the_default_listener_needs_no_configuration(self) -> None:
        direct = {name: row for name, row in VENUES.items() if not name.startswith("P06")}
        with _Configured(None):
            await self._assert_served(_general_routes(), direct)

    async def test_the_absent_origin_reads_serve_every_deployment(self) -> None:
        routes = [route for route in _register_everything() if route.path in ABSENT_ORIGIN_ROUTES]
        with _Configured(PROXY_ORIGIN):
            await self._assert_served(routes, VENUES, absent_origin=True)


class PrivateRoutesRequireQualifiedLoopbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.facts = server_facts(listen="127.0.0.1,::1")
        self.public_configured = False
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
            public_origins=lambda: self.public_configured,
            filesystem=filesystem,
        )
        self.projects = Mock(spec=ProjectDocumentService)
        self.projects.authoring = Mock(spec=["configure_editor_recovery"])
        self.projects.production = Mock(spec=["configure_editor_recovery"])
        self.recovery = EditorRecoveryService(self.projects, owner_port=port)
        self.projects.reset_mock()
        self.workspace = WorkspaceStateService(
            owner_port=port, collector=lambda: self.fail("no metadata collection before parsing")
        )
        self.retained = RetainedAssetService(
            owner_port=port,
            production=lambda: self.fail("no Production work before parsing"),
            media_runtime=lambda: self.fail("no media work before parsing"),
        )
        for key, service in (
            (composition_root.EDITOR_RECOVERY, self.recovery),
            (composition_root.WORKSPACE_STATE, self.workspace),
            (composition_root.RETAINED_ASSETS, self.retained),
        ):
            self.addCleanup(service.close)
            substitution = composition_root.substituted(key, service)
            substitution.__enter__()
            self.addCleanup(substitution.__exit__, None, None, None)
        all_routes = _register_everything()
        self.routes = {
            (route.method, route.path): route
            for route in all_routes
            if (route.method, route.path) in PRIVATE_ROUTES
        }
        self.assertEqual(set(self.routes), set(PRIVATE_ROUTES))
        self.addCleanup(self._assert_no_work)

    def _assert_no_work(self) -> None:
        self.assertFalse((self.root / "private").exists())
        self.assertIsNone(self.recovery._store)
        self.assertIsNone(self.workspace._store)
        self.assertIsNone(self.retained._store)
        self.assertFalse(self.recovery.writer_alive)
        self.assertFalse(self.workspace.sampler_alive)
        self.assertEqual(self.projects.mock_calls, [])

    def _assert_response(self, response: Any, status: int, code: str) -> None:
        self.assertEqual(response.status, status)
        body = json.loads(response.body) if isinstance(response.body, bytes) else response.body
        self.assertEqual(body.get("code", body.get("error")), code)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self._assert_no_work()

    async def test_each_private_route_reaches_its_parser_on_every_qualified_loopback(self) -> None:
        self.assertEqual(len(LOOPBACK_VENUES), 6)
        with _Configured(None):
            for name, (sockname, secure, host, origin) in LOOPBACK_VENUES.items():
                peer = (sockname[0], 5555, *sockname[2:])
                for key, route in self.routes.items():
                    with self.subTest(venue=name, path=route.path):
                        before = len(self.qualifications)
                        request = _admissible(host, origin, sockname, secure=secure)
                        request.transport = _PeerTransport(sockname, peer, secure=secure)
                        with _NotedRefusals() as noted:
                            response = await route.handler(request)
                        self.assertEqual(noted.reasons, [])
                        self._assert_response(response, 400, PRIVATE_ROUTES[key])
                        self.assertEqual(len(self.qualifications), before + 1)

    async def _refuse_before_body(
        self, route: Any, venue: tuple[Any, ...], peer: object, *, policy: bool = False
    ) -> None:
        sockname, secure, host, origin = venue
        request = _Refused(_headers(host, origin), _PeerTransport(sockname, peer, secure=secure))
        with _NotedRefusals() as noted:
            response = await route.handler(request)
        self.assertEqual(noted.reasons, [])
        code = (
            "origin_rejected"
            if "retained-assets" in route.path and not policy
            else "host_unqualified"
        )
        self._assert_response(response, 403, code)
        allowed = ADMISSION_ATTRIBUTES
        if policy and "retained-assets" in route.path:
            allowed |= {"query_string"}
        self.assertLessEqual(set(request.touched), allowed)
        self.assertEqual(self.qualifications, [])

    async def test_missing_and_foreign_peers_are_refused_on_every_loopback(self) -> None:
        with _Configured(None):
            for name, venue in LOOPBACK_VENUES.items():
                for peer in (None, ("192.0.2.1", 5555)):
                    for route in self.routes.values():
                        with self.subTest(venue=name, peer=peer, path=route.path):
                            await self._refuse_before_body(route, venue, peer)

    async def test_a_loopback_peer_does_not_qualify_a_lan_listener(self) -> None:
        with _Configured(None):
            for name, venue in VENUES.items():
                if not name.startswith("P05"):
                    continue
                for route in self.routes.values():
                    with self.subTest(venue=name, path=route.path):
                        await self._refuse_before_body(route, venue, ("127.0.0.1", 5555))

    async def test_unsupported_host_policy_is_refused_before_body_or_filesystem(self) -> None:
        cases: tuple[tuple[str, tuple[Any, ...], dict[str, Any], bool], ...] = (
            ("proxy", VENUES["P06 configured proxy"], {}, True),
            ("configured direct", VENUES[REFERENCE_VENUE], {}, True),
            ("multi-user", VENUES[REFERENCE_VENUE], {"multi_user": True}, False),
            ("CORS", VENUES[REFERENCE_VENUE], {"enable_cors_header": "*"}, False),
            ("mixed listener", VENUES[REFERENCE_VENUE], {"listen": "127.0.0.1,0.0.0.0"}, False),
        )
        for name, venue, overrides, configured in cases:
            self.facts = server_facts(**overrides)
            self.public_configured = configured
            with _Configured(PROXY_ORIGIN if configured else None):
                for route in self.routes.values():
                    with self.subTest(policy=name, path=route.path):
                        await self._refuse_before_body(
                            route, venue, ("127.0.0.1", 5555), policy=True
                        )


class NoDeploymentActsForAnotherTests(unittest.IsolatedAsyncioTestCase):
    async def _refused(
        self,
        route: Any,
        headers: dict[str, list[str]],
        transport: ListenerTransport,
        reason: AdmissionReason,
    ) -> None:
        request = _Refused(headers, transport)
        with _NotedRefusals() as noted:
            response = await route.handler(request)
        self.assertEqual(getattr(response, "status", None), 403)
        self.assertEqual(noted.reasons, [reason])
        # N06: nothing beyond the admission facts was read -- no body, no query, no path match,
        # no prelude input -- before the request was refused.
        self.assertLessEqual(set(request.touched), ADMISSION_ATTRIBUTES)

    async def test_a_trusted_alias_never_authorizes_another(self) -> None:
        # N01: each origin is legitimate for its own page; here it addresses another venue.
        routes = _register_everything()
        with _Configured(PROXY_ORIGIN):
            for venue, (sockname, secure, host, own) in VENUES.items():
                for other, (_s, _t, _h, foreign) in VENUES.items():
                    if foreign == own:
                        continue
                    for route in routes:
                        with self.subTest(venue=venue, origin_of=other, path=route.path):
                            await self._refused(
                                route,
                                _headers(host, foreign),
                                ListenerTransport(sockname, secure=secure),
                                AdmissionReason.ORIGIN_MISMATCH,
                            )

    async def test_a_page_on_the_old_port_cannot_act_on_a_moved_listener(self) -> None:
        for route in _register_everything():
            with self.subTest(path=route.path):
                await self._refused(
                    route,
                    _headers("127.0.0.1:8000", "http://127.0.0.1:8188"),
                    ListenerTransport(("127.0.0.1", 8000)),
                    AdmissionReason.ORIGIN_MISMATCH,
                )

    async def test_a_rebinding_page_is_refused_although_its_headers_agree(self) -> None:
        # N03: a hostile name resolved to this machine sends agreeing Host and Origin.
        for route in _register_everything():
            for origin in ("http://evil.example:8188", None):
                if origin is None and route.path not in ABSENT_ORIGIN_ROUTES:
                    continue
                with self.subTest(path=route.path, origin=origin):
                    await self._refused(
                        route,
                        _headers("evil.example:8188", origin),
                        ListenerTransport(),
                        AdmissionReason.HOST_UNTRUSTED,
                    )

    async def test_a_suffix_of_a_configured_name_is_not_that_name(self) -> None:
        with _Configured(PROXY_ORIGIN):
            for route in _register_everything():
                with self.subTest(path=route.path):
                    await self._refused(
                        route,
                        _headers(
                            "h3-proxy.localhost.evil.example:8443",
                            "https://h3-proxy.localhost.evil.example:8443",
                        ),
                        ListenerTransport(),
                        AdmissionReason.HOST_UNTRUSTED,
                    )

    async def test_forwarded_headers_select_nothing(self) -> None:
        # N04: a direct caller claims the proxy's authority through forwarding headers.
        forwarded = {
            "X-Forwarded-Host": ["h3-proxy.localhost:8443"],
            "X-Forwarded-Proto": ["https"],
            "X-Forwarded-For": ["127.0.0.1"],
            "Forwarded": ["host=h3-proxy.localhost:8443;proto=https"],
        }
        with _Configured(PROXY_ORIGIN):
            for route in _register_everything():
                with self.subTest(path=route.path):
                    await self._refused(
                        route,
                        _headers("127.0.0.1:8188", PROXY_ORIGIN, extra=forwarded),
                        ListenerTransport(),
                        AdmissionReason.ORIGIN_MISMATCH,
                    )

    async def test_invalid_configuration_configures_nothing(self) -> None:
        # N05: a partially valid list is not partially applied; the direct listener still works.
        routes = _register_everything()
        with _Configured(PROXY_ORIGIN + ",*"):
            for route in routes:
                with self.subTest(path=route.path):
                    await self._refused(
                        route,
                        _headers("h3-proxy.localhost:8443", PROXY_ORIGIN),
                        ListenerTransport(),
                        AdmissionReason.HOST_UNTRUSTED,
                    )
                    with _NotedRefusals() as noted:
                        await route.handler(
                            _admissible(
                                "127.0.0.1:8188", "http://127.0.0.1:8188", ("127.0.0.1", 8188)
                            )
                        )
                    self.assertEqual(noted.reasons, [])

    async def test_without_the_accepting_socket_nothing_is_admitted(self) -> None:
        for route in _register_everything():
            for transport in (None, SimpleNamespace(), ListenerTransport(None)):
                with self.subTest(path=route.path, transport=transport):
                    headers = _headers("127.0.0.1:8188", "http://127.0.0.1:8188")
                    request = _Refused(headers, transport)  # type: ignore[arg-type]
                    with _NotedRefusals() as noted:
                        response = await route.handler(request)
                    self.assertEqual(getattr(response, "status", None), 403)
                    self.assertEqual(noted.reasons, [AdmissionReason.AUTHORITY_UNAVAILABLE])

    async def test_a_missing_or_repeated_host_is_invalid(self) -> None:
        for route in _register_everything():
            for hosts in ([], ["127.0.0.1:8188", "127.0.0.1:8188"], ["127.0.0.1:8188, x"]):
                with self.subTest(path=route.path, hosts=hosts):
                    headers = _headers("unused", "http://127.0.0.1:8188")
                    headers["Host"] = hosts
                    await self._refused(
                        route, headers, ListenerTransport(), AdmissionReason.HOST_INVALID
                    )


class TheDiagnosticsNameACategoryAndNeverAValueTests(unittest.TestCase):
    def test_each_category_is_logged_once_without_request_values(self) -> None:
        hostile = SimpleNamespace(
            headers=_Headers(_headers("evil.example:8188", "http://evil.example:8188")),
            transport=ListenerTransport(),
        )
        with patch.object(seam, "_NOTED_REFUSALS", set()), _Configured(None):
            with self.assertLogs(seam.__name__, logging.WARNING) as logged:
                for _ in range(3):
                    self.assertFalse(seam.origin_accepted(hostile, seam.OriginRule.EXACT))
                seam.origin_accepted(
                    SimpleNamespace(
                        headers=_Headers(_headers("127.0.0.1:8188", "http://foreign.invalid")),
                        transport=ListenerTransport(),
                    ),
                    seam.OriginRule.EXACT,
                )
        self.assertEqual(len(logged.records), 2)
        text = "\n".join(logged.output)
        self.assertIn("reason=host_untrusted", text)
        self.assertIn(PUBLIC_ORIGINS_VARIABLE, text)
        self.assertIn("reason=origin_mismatch", text)
        for value in ("evil", "foreign", "8188", "127.0.0.1"):
            self.assertNotIn(value, text)

    def test_the_configuration_is_read_once_and_an_invalid_one_is_reported_by_category(
        self,
    ) -> None:
        private_looking = "https://internal-build-17.corp.example,https://*.corp.example"
        with patch.object(seam, "_PUBLIC_ORIGINS", None):
            with patch.dict(os.environ, {PUBLIC_ORIGINS_VARIABLE: private_looking}):
                with self.assertLogs(seam.__name__, logging.WARNING) as logged:
                    first = seam.public_origins()
            with patch.dict(os.environ, {PUBLIC_ORIGINS_VARIABLE: PROXY_ORIGIN}):
                second = seam.public_origins()
        self.assertIs(first, second)
        self.assertEqual(first.origins, ())
        self.assertEqual(len(logged.records), 1)
        self.assertIn("reason=malformed entries=2", logged.output[0])
        self.assertNotIn("corp", logged.output[0])

    def test_unset_and_valid_configuration_log_nothing(self) -> None:
        for raw in (None, PROXY_ORIGIN):
            with self.subTest(raw=raw), patch.object(seam, "_PUBLIC_ORIGINS", None):
                environment = {} if raw is None else {PUBLIC_ORIGINS_VARIABLE: raw}
                with patch.dict(os.environ, environment, clear=False):
                    if raw is None:
                        os.environ.pop(PUBLIC_ORIGINS_VARIABLE, None)
                    with self.assertNoLogs(seam.__name__, logging.WARNING):
                        seam.public_origins()


if __name__ == "__main__":
    unittest.main()
