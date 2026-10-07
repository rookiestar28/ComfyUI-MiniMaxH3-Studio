"""Fixture-backed Python doubles for governed ComfyUI host seams.

Tests may provide synthetic return payloads, but the module/member containers are
created only after the tracked HC-09 census and shape fixture validate.  This is
test support: it never imports or contacts a ComfyUI host.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from functools import lru_cache
from pathlib import Path
from types import ModuleType, SimpleNamespace

from comfyui_h3_context.core.host_seam_contract import (
    HostSeamContract,
    HostSeamElementKind,
    HostSeamKind,
    HostSeamReadiness,
    parse_host_seam_contract,
)

ROOT = Path(__file__).resolve().parents[1]
CENSUS = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_census_v1.json"
FIXTURE = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json"


@lru_cache(maxsize=1)
def _contract() -> HostSeamContract:
    return parse_host_seam_contract(
        json.loads(CENSUS.read_text(encoding="utf-8")),
        json.loads(FIXTURE.read_text(encoding="utf-8")),
    )


def _require_shape(
    seam_id: str,
    *,
    kind: HostSeamKind,
    element_kind: HostSeamElementKind,
    readiness: HostSeamReadiness = HostSeamReadiness.READY,
) -> None:
    try:
        row = next(row for row in _contract().rows if row.seam_id == seam_id)
    except StopIteration as error:
        raise AssertionError(f"unknown governed backend host seam: {seam_id}") from error
    if row.shape.kind is not kind or row.shape.element_kind is not element_kind:
        raise AssertionError(f"tracked host fixture shape disagrees for {seam_id}")
    if readiness not in row.census.readiness_states:
        raise AssertionError(f"unsupported host readiness double for {seam_id}")


class InstalledHostModules(AbstractContextManager["InstalledHostModules"]):
    """Temporarily install factory-created host modules, restoring prior state."""

    def __init__(self, **modules: ModuleType) -> None:
        self._modules = modules
        self._saved: dict[str, ModuleType | None] = {}

    def __enter__(self) -> InstalledHostModules:
        for name, module in self._modules.items():
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc: object) -> None:
        for name, previous in self._saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def host_nodes_module(*names: str) -> ModuleType:
    """Create the recorded node-class/display mapping container shape."""

    _require_shape(
        "backend.node_class_mappings",
        kind=HostSeamKind.MAPPING,
        element_kind=HostSeamElementKind.NODE_CLASS,
    )
    _require_shape(
        "backend.node_display_name_mappings",
        kind=HostSeamKind.MAPPING,
        element_kind=HostSeamElementKind.DISPLAY_NAME,
    )
    module = ModuleType("nodes")
    module.NODE_CLASS_MAPPINGS = {name: object for name in names}  # type: ignore[attr-defined]
    module.NODE_DISPLAY_NAME_MAPPINGS = {name: name for name in names}  # type: ignore[attr-defined]
    return module


def host_folder_paths_module(
    *,
    inventory: Mapping[str, object] | None = None,
    resolver: Callable[[str], object] | None = None,
) -> ModuleType:
    """Create the recorded callable slot with a test-owned bounded result."""

    _require_shape(
        "backend.folder_paths.get_filename_list",
        kind=HostSeamKind.CALLABLE,
        element_kind=HostSeamElementKind.PATH_STRING,
    )
    if inventory is not None and resolver is not None:
        raise ValueError("choose either inventory or resolver")
    values = dict(inventory or {})
    callback = resolver or (lambda folder: values.get(folder, []))
    module = ModuleType("folder_paths")
    module.get_filename_list = callback  # type: ignore[attr-defined]
    return module


def host_prompt_server_module(routes: object, *, available: bool = True) -> ModuleType:
    """Create the recorded PromptServer route-registry shape or unavailable state."""

    readiness = HostSeamReadiness.READY if available else HostSeamReadiness.UNAVAILABLE
    _require_shape(
        "backend.prompt_server.routes",
        kind=HostSeamKind.ROUTE_REGISTRY,
        element_kind=HostSeamElementKind.ROUTE,
        readiness=readiness,
    )
    module = ModuleType("server")
    if available:
        module.PromptServer = SimpleNamespace(  # type: ignore[attr-defined]
            instance=SimpleNamespace(routes=routes)
        )
    return module


def reset_fixture_cache_for_test() -> None:
    """Reset only the pure tracked-contract cache for a test process."""

    _contract.cache_clear()


__all__ = [
    "InstalledHostModules",
    "host_folder_paths_module",
    "host_nodes_module",
    "host_prompt_server_module",
    "reset_fixture_cache_for_test",
]
