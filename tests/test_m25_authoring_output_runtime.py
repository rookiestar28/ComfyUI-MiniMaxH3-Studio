"""Output runtime composition on the media runtime binding and unsupported capability."""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.media_runtime_manager import MediaRuntimeManager
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MediaRuntimeResolution,
    ResolutionReason,
    ResolutionState,
    SourceKind,
)
from comfyui_h3_context.core.authoring_render_jobs import RenderJobFailure, RenderJobPhase


def test_package_entry_registers_output_routes_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comfyui_h3_context

    web = pytest.importorskip("aiohttp.web")
    module = runtime_module()
    table = web.RouteTableDef()
    owner = module.AuthoringOutputRuntime(None)
    monkeypatch.setattr(module, "host_web_and_routes", lambda: (web, table))
    monkeypatch.setattr(module, "_runtime", lambda: owner)
    try:
        importlib.reload(comfyui_h3_context)
        importlib.reload(comfyui_h3_context)
        assert {(route.method, route.path) for route in table} == {
            ("GET", "/h3-context/v1/authoring/output-capability"),
            ("POST", "/h3-context/v1/authoring/render"),
            ("GET", "/h3-context/v1/authoring/render/{handle}"),
            ("POST", "/h3-context/v1/authoring/render/{handle}/cancel"),
            ("GET", "/h3-context/v1/authoring/output/{handle}/preview"),
            ("GET", "/h3-context/v1/authoring/output/{handle}/download"),
        }
        assert len(table) == 6
        assert owner.registry() is None
    finally:
        owner.close()


def test_output_wire_fixture_matches_real_runtime_and_accepted_failure_enum() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/m25_19_output_contract_v1.json").read_text(
            encoding="utf-8"
        )
    )
    owner = runtime_module().AuthoringOutputRuntime(None)
    try:
        assert owner.capability() == fixture["capability"]
        assert [phase.value for phase in RenderJobPhase] == fixture["phases"]
        assert [failure.value for failure in RenderJobFailure] == fixture["failures"]
    finally:
        owner.close()


def runtime_module() -> Any:
    name = "comfyui_h3_context.adapters.comfyui_authoring_output_runtime"
    assert importlib.util.find_spec(name) is not None, "explicit output runtime is missing"
    return importlib.import_module(name)


class _Resolver:
    def __init__(self, result: MediaRuntimeResolution) -> None:
        self.result = result
        self.resolved = False

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        self.resolved = True
        return self.result

    def invalidate(self) -> None:
        self.resolved = False

    def peek(self) -> MediaRuntimeResolution | None:
        return self.result if self.resolved else None


class _Publisher:
    def publish(self, adapter: object) -> None:
        return None

    def clear(self, adapter: object) -> None:
        return None


def _unqualified(root: Path) -> MediaRuntimeResolution:
    return MediaRuntimeResolution(
        ResolutionState.LOCATED,
        ResolutionReason.PAIR_ADMITTED,
        SourceKind.MANAGED,
        ffmpeg_path=(root / "ffmpeg.exe").resolve(),
        ffprobe_path=(root / "ffprobe.exe").resolve(),
        scratch_root=(root / "scratch").resolve(),
        ffmpeg_identity=(1, 2, 3, 4),
        ffprobe_identity=(1, 2, 3, 5),
    )


def test_unbound_or_unqualified_output_runtime_is_unsupported_without_allocating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runtime_module()

    def no_store(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("an unqualified runtime allocated a private store")

    monkeypatch.setattr(module, "RenderOutputStore", no_store)
    previous = composition_root.installed(composition_root.MEDIA_RUNTIME)
    missing = MediaRuntimeResolution(
        ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING
    )
    try:
        for resolution in (missing, _unqualified(tmp_path)):
            started: list[Callable[[], None]] = []
            manager = MediaRuntimeManager(
                resolver=_Resolver(resolution),  # type: ignore[arg-type]
                adapter_factory=lambda *_paths: object.__new__(QualifiedAVMediaAdapter),
                publisher=_Publisher(),
                thread_starter=started.append,
            )
            composition_root.install(composition_root.MEDIA_RUNTIME, manager)
            owner = module.build_authoring_output_runtime()
            try:
                assert owner.registry() is None
                # An event-loop route read asks for activation and never waits for it.
                assert owner._route_registry() is None
                assert len(started) == 1
                started.pop()()
                assert owner.registry() is None
                capability = owner.capability()
                assert capability["supported"] is False
                assert capability["schema"] == "h3.authoring.output_capability.v1"
                assert capability["max_output_bytes"] == 512 * 1024 * 1024
                assert capability["max_preview_bytes"] == 16 * 1024 * 1024
                assert capability["max_http_requests"] == 4
                assert "path" not in repr(owner)
                render = manager.readiness()["render"]
                if resolution is missing:
                    assert render == {"state": "setup_required", "reason": "supported_pair_missing"}
                else:
                    assert render == {
                        "state": "unavailable",
                        "reason": "render_qualification_unavailable",
                    }
            finally:
                manager.remove_consumer(owner)
                owner.close()
            assert owner.registry() is None
    finally:
        if previous is None:
            composition_root.reset(composition_root.MEDIA_RUNTIME)
        else:
            composition_root.install(composition_root.MEDIA_RUNTIME, previous)


def test_output_component_has_one_owning_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    component = getattr(composition_root, "AUTHORING_OUTPUT", None)
    assert component in composition_root.COMPONENTS, "output composition owner is missing"
    module = runtime_module()
    marker = object()
    monkeypatch.setattr(module, "build_authoring_output_runtime", lambda **_kw: marker)
    previous = composition_root.installed(component)
    composition_root.reset(component)
    try:
        assert composition_root.get(component) is marker
        assert composition_root.get(component) is marker
    finally:
        composition_root.reset(component)
        if previous is not None:
            composition_root.install(component, previous)
