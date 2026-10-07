"""M23-47: the owned HTTP edge, asserted once over every route instead of once per route.

Before this item there were nine per-route origin assertions and thirteen routes. Three of the four
routes those assertions did not cover had no origin rule at all, and nothing would have reported it:
a per-route test cannot fail for a route that never had one written. Everything here is therefore
written as a property of the whole registered surface, discovered by walking the adapters package
rather than by naming modules, so a route added later is covered the day it is added.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from deployment_request_doubles import (
    LOOPBACK_ORIGIN as OWNED_ORIGIN,
)
from deployment_request_doubles import (
    ListenerTransport,
    admission_headers,
)

import comfyui_h3_context.adapters as adapters_package
from comfyui_h3_context.adapters import comfyui_route_seam as seam
from comfyui_h3_context.adapters.comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_build_provenance import BUILD_PROVENANCE_ROUTE
from comfyui_h3_context.adapters.comfyui_generation_profile import GENERATION_PROFILE_ROUTE
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    RouteResult,
    content_length_verdict,
    origin_accepted,
    read_bounded_body,
    register_owned_route,
    route_method_available,
)
from comfyui_h3_context.adapters.managed_sequence_service import MANAGED_SEQUENCE_PROJECTION_ROUTE
from comfyui_h3_context.core.errors import OptionalDependencyError
from comfyui_h3_context.core.request_target import AdmissionReason
from scripts.hc_09_host_seam_test_double import host_prompt_server_module

#: Origins that are not this request's own target. Every request below addresses the default
#: listener (`Host: 127.0.0.1:8188` on a socket bound to it), so each of these is foreign to it --
#: including `localhost`, another port, IPv6 loopback and HTTPS, which are legitimate for their OWN
#: page (M23-57) but never authorize this one. The absent header is not here: it is a legitimate
#: same-origin GET and has its own assertion below.
FOREIGN_ORIGINS: tuple[list[str], ...] = (
    ["http://localhost:8188"],
    ["http://127.0.0.1:8189"],
    ["http://[::1]:8188"],
    ["https://127.0.0.1:8188"],
    ["http://127.0.0.1:8188/path"],
    [OWNED_ORIGIN, OWNED_ORIGIN],
    [OWNED_ORIGIN, "http://foreign.invalid"],
    [OWNED_ORIGIN + ",http://foreign.invalid"],
)

#: The two routes that answer an absent `Origin`, because a browser omits the header on a
#: same-origin GET and both are read-only GETs whose own page would otherwise be refused. Any
#: third entry is a deliberate widening and must carry its own reason; a POST may never appear.
ABSENT_ORIGIN_ROUTES = frozenset(
    {
        GENERATION_PROFILE_ROUTE,
        BUILD_PROVENANCE_ROUTE,
        MANAGED_SEQUENCE_PROJECTION_ROUTE,
        # Native output navigation is read-only and additionally rejects foreign fetch metadata.
        # M25-16: the capability projection is a same-origin fetch GET the browser sends without
        # an Origin header; it reads no workspace and starts no job.
        "/h3-context/v1/authoring/output-capability",
        "/h3-context/v1/authoring/render/{handle}",
        "/h3-context/v1/authoring/output/{handle}/preview",
        "/h3-context/v1/authoring/output/{handle}/download",
        # M25-31: the media runtime status and setup-job reads are same-origin fetch GETs the
        # browser sends without Origin; both reject foreign fetch metadata. Neither writes state or
        # downloads: status may run bounded local discovery, and the job read is a projection.
        "/h3-context/v1/media-runtime",
        "/h3-context/v1/media-runtime/setup/{job_id}",
    }
)


class _Routes(list[SimpleNamespace]):
    """The host router surface, recording every decoration in registration order."""

    def _decorate(self, method: str, path: str) -> Any:
        def decorate(handler: Any) -> Any:
            self.append(SimpleNamespace(method=method, path=path, handler=handler))
            return handler

        return decorate

    def get(self, path: str, *, allow_head: bool = True) -> Any:
        return self._decorate("GET", path)

    def post(self, path: str) -> Any:
        return self._decorate("POST", path)

    def delete(self, path: str) -> Any:
        return self._decorate("DELETE", path)


class _UnreadableContent:
    """A body that fails the test if anything reads it."""

    async def read(self, _limit: int) -> bytes:
        raise AssertionError("a refused request read its body")


class _ReadableContent:
    """A body a handler is meant to receive, for the cases that are not about the read at all."""

    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload, b""]

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0)


class _Headers:
    def __init__(self, origins: list[str]) -> None:
        self._values = admission_headers(origins)

    def getall(self, name: str, default: list[str]) -> list[str]:
        return list(self._values.get(name, default))


def _stream_response(status: int = 200, headers: object = None) -> SimpleNamespace:
    async def prepare(_request: object) -> None:
        return None

    async def write_eof() -> None:
        return None

    return SimpleNamespace(status=status, headers=headers, prepare=prepare, write_eof=write_eof)


def _web() -> SimpleNamespace:
    return SimpleNamespace(
        json_response=lambda value, status=200, headers=None, dumps=None: SimpleNamespace(
            status=status, body=value, headers=headers
        ),
        Response=lambda status=200, body=None, headers=None: SimpleNamespace(
            status=status, body=body, headers=headers
        ),
        StreamResponse=_stream_response,
    )


def _request(origins: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        content_type="application/json",
        content_length=1,
        content=_UnreadableContent(),
        headers=_Headers(origins),
        query_string="",
        transport=ListenerTransport(),
    )


class _NotedRefusals:
    """Records every admission refusal any edge makes, through the seam's one diagnostic hook."""

    def __init__(self) -> None:
        self.reasons: list[AdmissionReason] = []

    def __enter__(self) -> _NotedRefusals:
        self._patcher = patch.object(seam, "_note_refusal", self.reasons.append)
        self._patcher.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patcher.stop()


#: The only adapters allowed to be missing from the walk below, and the only exception that may
#: excuse them. Both need a module the host supplies (`comfy_api`, `comfy_execution.utils`) and
#: neither owns a route. A blanket `except Exception` here would let a route-owning module drop out
#: of every assertion in this file the day it stops importing, which is exactly the silence this
#: module's docstring promises not to have -- so anything else is a failure, loudly.
IMPORT_MAY_FAIL: frozenset[str] = frozenset({"comfyui", "comfyui_execution"})


def _adapter_modules() -> Iterator[ModuleType]:
    for info in pkgutil.iter_modules(adapters_package.__path__):
        try:
            module = importlib.import_module(f"{adapters_package.__name__}.{info.name}")
        except OptionalDependencyError:
            if info.name not in IMPORT_MAY_FAIL:
                raise
            continue
        yield module


def _registrars() -> list[tuple[ModuleType, str]]:
    # CRITICAL: `_registered`, not `_route_registered`. The lease registrar is plural
    # (`ensure_authoring_media_lease_routes_registered`), and the narrower suffix silently left its
    # two capability-bearing routes out of every invariant in this file until M23-57.
    # `test_the_walk_finds_every_registrar_the_package_calls` pins the set against the package.
    found = [
        (module, name)
        for module in _adapter_modules()
        for name in dir(module)
        if name.startswith("ensure_")
        and name.endswith("_registered")
        and getattr(getattr(module, name), "__module__", None) == module.__name__
    ]
    found.sort(key=lambda pair: (pair[0].__name__, pair[1]))
    return found


def _register_everything() -> _Routes:
    """Register every owned route this package publishes, against one fake host."""

    routes = _Routes()
    registrars = _registrars()
    server = host_prompt_server_module(routes)
    aiohttp = ModuleType("aiohttp")
    aiohttp.__dict__["web"] = _web()
    # A module global is a `__dict__` entry, and going through it keeps both the linter and the
    # type checker satisfied: `ModuleType` declares no such attribute, so attribute access is an
    # error, and `setattr` on a literal name is a lint.
    saved = {
        module.__name__: module.__dict__["_ROUTE_REGISTERED"]
        for module, _ in registrars
        if "_ROUTE_REGISTERED" in module.__dict__
    }
    try:
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            for module, name in registrars:
                if "_ROUTE_REGISTERED" in module.__dict__:
                    module.__dict__["_ROUTE_REGISTERED"] = False
                if not getattr(module, name)():
                    raise AssertionError(f"{module.__name__}.{name} refused a free router")
    finally:
        # CRITICAL: these globals are process-wide. Leaving one flipped makes an unrelated adapter
        # test that reads it depend on whether this module ran first.
        for module, _ in registrars:
            if module.__name__ in saved:
                module.__dict__["_ROUTE_REGISTERED"] = saved[module.__name__]
    # The lease edge builds its responses through the seam's lazy `aiohttp.web` lookup at request
    # time, so every handler runs with the same host double installed.
    for route in routes:
        route.handler = _under_host(route.handler, aiohttp)
    return routes


def _under_host(handler: Any, aiohttp: ModuleType) -> Any:
    async def call(request: Any) -> Any:
        with patch.dict(sys.modules, {"aiohttp": aiohttp}):
            return await handler(request)

    return call


ADAPTER_ROOT = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "adapters"

#: The seam owns the HTTP edge. These two modules do not, and the exception is deliberate: both
#: drive request and work deadlines, refuse `Range` and query strings, watch for a closing transport
#: and answer with streamed or deliberately empty bodies. Flattening them into the seam would delete
#: behaviour rather than share it. Final-output transport is a separate owned streaming edge:
#: it validates complete artifacts before ranges and retains admission through disconnect cleanup.
#: It still delegates origin policy to this seam; ordinary JSON adapters cannot add private edges.
EDGE_OWNERS = frozenset(
    {
        "comfyui_route_seam.py",
        "comfyui_media_preview.py",
        "comfyui_authoring_media_preview.py",
        "comfyui_authoring_output.py",
    }
)


def _adapter_sources() -> Iterator[tuple[str, str]]:
    for path in sorted(ADAPTER_ROOT.glob("*.py")):
        yield path.name, path.read_text(encoding="utf-8")


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


class NoAdapterImplementsItsOwnHttpEdgeTests(unittest.TestCase):
    """AC 3: a route cannot reintroduce a private edge without failing here."""

    def test_only_the_edge_owners_reach_the_optional_host_modules(self) -> None:
        for name, text in _adapter_sources():
            with self.subTest(module=name):
                names = _string_constants(ast.parse(text)) & {"server", "aiohttp"}
                if names:
                    self.assertIn(
                        name,
                        EDGE_OWNERS,
                        f"{name} looks up an optional host module outside the shared seam",
                    )

    def test_only_the_edge_owners_read_the_origin_header(self) -> None:
        for name, text in _adapter_sources():
            with self.subTest(module=name):
                if "Origin" in _string_constants(ast.parse(text)):
                    self.assertIn(name, EDGE_OWNERS, f"{name} decides its own origin rule")

    def test_only_the_edge_owners_serialize_a_response(self) -> None:
        for name, text in _adapter_sources():
            with self.subTest(module=name):
                tree = ast.parse(text)
                serializers = {
                    node.attr
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "web"
                }
                if serializers:
                    self.assertIn(name, EDGE_OWNERS, f"{name} serializes its own response")

    def test_every_seam_route_declares_its_policy_as_data(self) -> None:
        # `architecture_fitness` refuses a registration whose policy or handler is not a named
        # symbol in the same module, which is what makes the route inventory mechanical rather than
        # a hand-kept list. Exercising it here keeps that failure attached to this item's subject.
        import scripts.architecture_fitness as fitness

        for name, text in _adapter_sources():
            with self.subTest(module=name):
                tree = ast.parse(text)
                declared = fitness._policy_declarations(tree)
                for policy_symbol, handler_symbol in fitness._seam_registrations(tree):
                    self.assertIn(policy_symbol, declared)
                    self.assertIsNotNone(fitness._function_named(tree, handler_symbol))


class TheOwnedSurfaceIsDiscoverableTests(unittest.TestCase):
    def test_the_walk_finds_every_registrar_the_package_calls(self) -> None:
        package = Path(__file__).resolve().parents[1] / "comfyui_h3_context" / "__init__.py"
        called = {
            node.func.id
            for node in ast.walk(ast.parse(package.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("ensure_")
        }
        self.assertEqual({name for _, name in _registrars()}, called)
        self.assertIn("ensure_authoring_media_lease_routes_registered", called)

    def test_every_registrar_publishes_at_least_one_route(self) -> None:
        routes = _register_everything()
        self.assertGreaterEqual(len(routes), len(_registrars()))
        self.assertEqual(
            len({(route.method, route.path) for route in routes}),
            len(routes),
            "one method and path is claimed exactly once",
        )

    def test_the_absent_origin_exception_is_read_only_and_named(self) -> None:
        methods = {
            route.path: route.method
            for route in _register_everything()
            if route.path in ABSENT_ORIGIN_ROUTES
        }
        self.assertEqual(set(methods), set(ABSENT_ORIGIN_ROUTES))
        self.assertEqual(set(methods.values()), {"GET"})


class EveryOwnedRouteRefusesAForeignOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_registered_route_admits_an_origin_that_is_not_its_target(self) -> None:
        routes = _register_everything()
        for route in routes:
            for origins in FOREIGN_ORIGINS:
                with self.subTest(path=route.path, method=route.method, origins=origins):
                    with _NotedRefusals() as noted:
                        response = await route.handler(_request(origins))
                    self.assertEqual(
                        getattr(response, "status", None),
                        403,
                        f"{route.method} {route.path} admitted {origins}",
                    )
                    # The refusal is the origin rule, not a request double missing its facts.
                    self.assertEqual(len(noted.reasons), 1)
                    self.assertIn(
                        noted.reasons[0],
                        {AdmissionReason.ORIGIN_MISMATCH, AdmissionReason.ORIGIN_INVALID},
                    )

    async def test_an_absent_origin_is_refused_everywhere_but_the_two_read_only_gets(self) -> None:
        for route in _register_everything():
            with self.subTest(path=route.path, method=route.method):
                if route.path in ABSENT_ORIGIN_ROUTES:
                    continue
                with _NotedRefusals() as noted:
                    response = await route.handler(_request([]))
                self.assertEqual(getattr(response, "status", None), 403)
                self.assertEqual(noted.reasons, [AdmissionReason.ORIGIN_MISSING])

    async def test_every_route_admits_its_own_target(self) -> None:
        # The positive control for the two refusals above: without it, a route that refused
        # everything -- a double missing its transport, say -- would satisfy both.
        for route in _register_everything():
            with self.subTest(path=route.path, method=route.method):
                request = _request([OWNED_ORIGIN])
                request.content = _ReadableContent(b"{}")
                request.content_length = 2
                with _NotedRefusals() as noted:
                    if route.path in {
                        "/h3-context/workspace-state",
                        "/h3-context/retained-assets",
                        "/h3-context/retained-assets/preview",
                        "/h3-context/project-recovery",
                    }:
                        from comfyui_h3_context.adapters import composition_root
                        from comfyui_h3_context.adapters.recovery_owner import RecoveryOwnerPort
                        from comfyui_h3_context.adapters.retained_asset_service import (
                            RetainedAssetService,
                        )
                        from comfyui_h3_context.adapters.workspace_state_service import (
                            WorkspaceStateService,
                        )

                        # Origin admission is still the subject; provide real qualified owner
                        # facts for this stricter capability rather than allowing any 403.
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory).absolute()
                            host = SimpleNamespace(
                                private_root=lambda root=root: root / "private",
                                served_roots=lambda root=root: (
                                    root / "input",
                                    root / "output",
                                    root / "temp",
                                ),
                            )
                            port = RecoveryOwnerPort(
                                host=host,
                                host_facts=lambda: (
                                    SimpleNamespace(
                                        listen="127.0.0.1",
                                        multi_user=False,
                                        enable_cors_header=None,
                                    ),
                                    SimpleNamespace(users={"default": "default"}),
                                ),
                                public_origins=lambda: False,
                                filesystem=lambda _path: True,
                            )
                            service: Any
                            if route.path == "/h3-context/workspace-state":
                                service = WorkspaceStateService(
                                    owner_port=port, collector=lambda: ()
                                )
                                owner_key = composition_root.WORKSPACE_STATE
                            elif route.path == "/h3-context/project-recovery":
                                from comfyui_h3_context.adapters.editor_recovery_service import (
                                    EditorRecoveryService,
                                )
                                from comfyui_h3_context.adapters.project_document_service import (
                                    ProjectDocumentService,
                                )

                                service = EditorRecoveryService(
                                    ProjectDocumentService(
                                        ProductionWorkspaceRegistry(
                                            seed_claim=lambda *_args: self.fail(
                                                "origin control must not claim Context"
                                            )
                                        ),
                                        AuthoringWorkspaceRegistry(),
                                    ),
                                    owner_port=port,
                                )
                                owner_key = composition_root.EDITOR_RECOVERY
                            else:
                                service = RetainedAssetService(
                                    owner_port=port,
                                    production=lambda: self.fail(
                                        "origin control must not generate"
                                    ),
                                )
                                owner_key = composition_root.RETAINED_ASSETS
                            request.transport = SimpleNamespace(
                                get_extra_info=lambda name: {
                                    "sockname": ("127.0.0.1", 8188),
                                    "peername": ("127.0.0.1", 5555),
                                }.get(name),
                                is_closing=lambda: False,
                            )
                            try:
                                with composition_root.substituted(owner_key, service):
                                    response = await route.handler(request)
                            finally:
                                service.close()
                    else:
                        response = await route.handler(request)
                self.assertEqual(noted.reasons, [], f"{route.method} {route.path} refused")
                # An edge that refuses on its own never reaches the hook; its 403 must show here.
                self.assertNotEqual(getattr(response, "status", None), 403)

    async def test_the_read_only_gets_still_answer_their_own_page(self) -> None:
        routes = [route for route in _register_everything() if route.path in ABSENT_ORIGIN_ROUTES]
        self.assertEqual(len(routes), len(ABSENT_ORIGIN_ROUTES))
        for route in routes:
            for origins in ([], [OWNED_ORIGIN]):
                with self.subTest(path=route.path, origins=origins):
                    response = await route.handler(_request(origins))
                    self.assertNotEqual(getattr(response, "status", None), 403)


#: The four owners that carry request-id replay protection, each with the refusal it answers a
#: same-id-different-intent retry with. They are deliberately three and not one: the entry shapes,
#: the mismatch statuses, and above all the capacity behaviours differ in ways no single ledger can
#: express without five policy hooks -- authoring evicts its oldest entry, production refuses with a
#: 409 that carries a projection to reconcile against, and the coordinator refuses with a 429 under
#: both an entry count and a retained-bytes budget. Unifying the three status codes is a wire change
#: `M23-34` owns along with the problem-details envelope; what is asserted here is that there are
#: exact closed set, so another owner cannot quietly invent another behaviour.
REPLAY_LEDGERS = {
    "comfyui_authoring_workspace.py": (422, "request_replay_mismatch"),
    "comfyui_production_workspace.py": (409, "request_id_conflict"),
    "comfyui_sequence_coordinator.py": (409, "request_id_conflict"),
    "managed_sequence_service.py": (409, "request_id_conflict"),
}


class ReplayProtectionLivesInAClosedSetTests(unittest.TestCase):
    """AC 4: the four behaviours are preserved, and another cannot appear unnoticed."""

    def test_no_module_outside_the_three_carries_a_request_id_ledger(self) -> None:
        product = Path(__file__).resolve().parents[1] / "comfyui_h3_context"
        carrying = {
            path.name
            for path in sorted(product.rglob("*.py"))
            if "self._ledger" in (text := path.read_text(encoding="utf-8")) and "request_id" in text
        }
        self.assertEqual(carrying, set(REPLAY_LEDGERS))

    def test_each_of_the_three_still_names_its_own_refusal(self) -> None:
        for name, (status, code) in REPLAY_LEDGERS.items():
            with self.subTest(module=name):
                text = (ADAPTER_ROOT / name).read_text(encoding="utf-8")
                self.assertIn(code, text)
                self.assertIn(str(status), text)


class TheSeamRefusesAnIncoherentPolicyTests(unittest.TestCase):
    def test_a_body_budget_belongs_to_exactly_the_body_carrying_methods(self) -> None:
        for method, max_bytes in (("POST", None), ("GET", 16), ("DELETE", 16)):
            with self.subTest(method=method, max_bytes=max_bytes):
                with self.assertRaises(ValueError):
                    RoutePolicy(
                        path="/x",
                        owner="o",
                        owner_attribute="_a",
                        method=method,
                        max_bytes=max_bytes,
                    )

    def test_an_unsupported_method_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            RoutePolicy(path="/x", owner="o", owner_attribute="_a", method="PUT", max_bytes=1)

    def test_declaring_a_typed_refusal_obliges_the_route_to_map_it(self) -> None:
        policy = RoutePolicy(
            path="/x",
            owner="o",
            owner_attribute="_a",
            max_bytes=16,
            refusals=(ValueError,),
        )

        async def handler(_payload: bytes, _context: object) -> RouteResult:
            return RouteResult(200, {})

        with self.assertRaises(ValueError):
            register_owned_route(policy, __name__, handler, lambda _status, _reason: None)


class AFailureIsBlamedOnWhoeverCausedItTests(unittest.IsolatedAsyncioTestCase):
    """A decode failure is the caller's fault only on a method that carries something to decode."""

    async def _drive(
        self,
        method: str,
        max_bytes: int | None,
        *,
        content: object = None,
        failure: BaseException | None = None,
    ) -> tuple[int, str]:
        routes = _Routes()
        policy = RoutePolicy(
            path="/h3-context/v1/test/blame",
            owner="owner",
            owner_attribute="_owned",
            method=method,
            max_bytes=max_bytes,
        )
        raised = failure or ValueError("something this process got wrong on its own")

        async def handler(_payload: bytes, _context: object) -> RouteResult:
            raise raised

        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            self.assertTrue(
                register_owned_route(
                    policy,
                    __name__,
                    handler,
                    lambda status, reason: {"status": status, "reason": reason},
                )
            )
        request = _request([OWNED_ORIGIN])
        if max_bytes is not None:
            request.content = content if content is not None else _ReadableContent(b"{}")
            request.content_length = 2
        response = await routes[0].handler(request)
        body = response.body
        assert isinstance(body, dict)
        return response.status, str(body["reason"])

    async def test_a_post_blames_the_body_it_was_given(self) -> None:
        self.assertEqual(await self._drive("POST", 16), (400, "invalid_request"))

    async def test_a_get_or_delete_blames_itself(self) -> None:
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(await self._drive(method, None), (500, "internal_failure"))

    async def test_a_body_reader_that_misbehaves_answers_the_generic_decode_refusal(self) -> None:
        # The wire this pins is a deliberate M23-47 generalization, recorded because it is the one
        # place the shared read changed an answer: `comfyui_provider_settings` raised its own
        # `request_malformed` here, and every other route already said `invalid_request`. Both are
        # 400, and no real `aiohttp.StreamReader.read` can reach it -- it always returns bytes. It
        # is pinned rather than left unobserved so a later change to `read_bounded_body`'s
        # exception type is a test failure instead of a silent wire move.
        response = await self._drive("POST", 16, content=SimpleNamespace(read=_returns_text))
        self.assertEqual(response, (400, "invalid_request"))

    async def test_a_failure_outside_the_decode_trio_is_this_process_own(self) -> None:
        # CRITICAL: the seam reads `TypeError`, `ValueError` and `UnicodeDecodeError` as "the caller
        # sent something undecodable". A handler that raises one of those for an internal invariant
        # violation therefore blames the caller for a request that was correct. Raising anything
        # else reaches the blanket guard, which is the honest 500 -- this is why
        # `comfyui_provider_settings._rejection_status` converts its enum lookup failure.
        response = await self._drive("POST", 16, failure=RuntimeError("our own invariant"))
        self.assertEqual(response, (500, "internal_failure"))

    async def test_a_typed_success_can_publish_one_immutable_response_header(self) -> None:
        routes = _Routes()
        policy = RoutePolicy(
            path="/h3-context/v1/test/etag",
            owner="owner",
            owner_attribute="_owned",
            method="GET",
            origin=OriginRule.EXACT_OR_ABSENT,
        )

        async def handler(_payload: bytes, _context: object) -> RouteResult:
            return RouteResult(200, {"ok": True}, (("ETag", "sha256:" + "a" * 64),))

        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
            self.assertTrue(
                register_owned_route(policy, __name__, handler, lambda _status, _reason: None)
            )
        response = await routes[0].handler(_request([]))
        self.assertEqual(response.headers, {"ETag": "sha256:" + "a" * 64})


class TheOriginPrimitiveIsExactTests(unittest.TestCase):
    def test_a_second_origin_alongside_the_owned_one_is_still_foreign(self) -> None:
        for rule in OriginRule:
            for origins in FOREIGN_ORIGINS:
                with self.subTest(rule=rule, origins=origins):
                    self.assertFalse(origin_accepted(_request(origins), rule))

    def test_a_header_surface_that_is_not_a_list_is_refused(self) -> None:
        broken = SimpleNamespace(
            headers=SimpleNamespace(getall=lambda _name, _default: OWNED_ORIGIN),
            transport=ListenerTransport(),
        )
        for rule in OriginRule:
            with self.subTest(rule=rule):
                self.assertFalse(origin_accepted(broken, rule))


class TheDeclaredLengthAndTheReadAgreeTests(unittest.IsolatedAsyncioTestCase):
    def test_a_nonsense_declared_length_is_malformed_rather_than_oversized(self) -> None:
        for declared in (-1, True, 4.0, "16"):
            with self.subTest(declared=declared):
                request = SimpleNamespace(content_length=declared)
                self.assertEqual(content_length_verdict(request, 16), "malformed")
        self.assertEqual(
            content_length_verdict(SimpleNamespace(content_length=17), 16), "too_large"
        )
        self.assertEqual(
            content_length_verdict(SimpleNamespace(content_length=None), 16), "accepted"
        )
        self.assertEqual(content_length_verdict(SimpleNamespace(content_length=16), 16), "accepted")

    async def test_a_chunked_body_is_bounded_while_it_is_read(self) -> None:
        class _Chunks:
            def __init__(self, *chunks: bytes) -> None:
                self.limits: list[int] = []
                self._chunks = [*chunks, b""]

            async def read(self, limit: int) -> bytes:
                self.limits.append(limit)
                return self._chunks.pop(0)

        # CRITICAL: a chunked request declares no length at all, so the budget can only be enforced
        # against what actually arrives. Each read asks for exactly one byte more than the remaining
        # budget, which is what makes an over-budget body detectable rather than merely truncated.
        content = _Chunks(b"abc", b"defg")
        request = SimpleNamespace(content=content)
        self.assertEqual(await read_bounded_body(request, 16), b"abcdefg")
        self.assertEqual(content.limits, [17, 14, 10])

        over = SimpleNamespace(content=_Chunks(b"x" * 17))
        self.assertIsNone(await read_bounded_body(over, 16))

    async def test_a_reader_that_returns_something_other_than_bytes_fails_closed(self) -> None:
        request = SimpleNamespace(content=SimpleNamespace(read=_returns_text))
        with self.assertRaises(ValueError):
            await read_bounded_body(request, 16)


class APairedRegistrarIsAllOrNothingTests(unittest.TestCase):
    """One adapter owns two methods on one path; a half-registered pair is unremovable."""

    def test_a_router_that_cannot_serve_the_second_method_registers_neither(self) -> None:
        from comfyui_h3_context.adapters import comfyui_provider_settings as provider

        class _PostOnlyRoutes(list[SimpleNamespace]):
            """A host router that cannot serve DELETE at all."""

            def post(self, path: str) -> Any:
                def decorate(handler: Any) -> Any:
                    self.append(SimpleNamespace(method="POST", path=path, handler=handler))
                    return handler

                return decorate

        routes = _PostOnlyRoutes()
        server = host_prompt_server_module(routes)
        aiohttp = ModuleType("aiohttp")
        aiohttp.__dict__["web"] = _web()
        saved = provider.__dict__["_ROUTE_REGISTERED"]
        try:
            with patch.dict(sys.modules, {"server": server, "aiohttp": aiohttp}):
                provider.__dict__["_ROUTE_REGISTERED"] = False
                self.assertFalse(provider.ensure_provider_settings_route_registered())
        finally:
            provider.__dict__["_ROUTE_REGISTERED"] = saved
        # The POST half must not have been claimed on the way to discovering the DELETE half
        # cannot be: this package has no way to withdraw a registration it already made.
        self.assertEqual(list(routes), [])

    def test_the_seam_reports_a_method_the_router_cannot_serve(self) -> None:
        policy = RoutePolicy(
            path="/h3-context/v1/test/pair",
            owner="owner",
            owner_attribute="_owned",
            method="DELETE",
        )
        self.assertTrue(route_method_available(_Routes(), policy))
        self.assertFalse(route_method_available(SimpleNamespace(), policy))
        self.assertFalse(route_method_available(SimpleNamespace(delete=None), policy))


class AnUnknownRejectionIsNotTheCallersFaultTests(unittest.TestCase):
    def test_a_rejection_outside_the_enum_is_raised_as_this_process_own_failure(self) -> None:
        from comfyui_h3_context.adapters.comfyui_provider_settings import _rejection_status

        with self.assertRaises(RuntimeError) as raised:
            _rejection_status("a_rejection_the_core_never_produces")
        # Not a ValueError: the seam would read that as the caller's undecodable body and answer
        # 400, telling a caller whose request was correct to change it.
        self.assertNotIsInstance(raised.exception, ValueError)


async def _returns_text(_limit: int) -> str:
    return "not bytes"


if __name__ == "__main__":
    unittest.main()
