"""The one place this process constructs its long-lived adapter registries.

Five adapter modules built their registry at import time and a sixth built its coordinator lazily
behind a double-checked lock, reaching into another module's private `_REGISTRY` to do it.

Import time is the wrong moment. Constructing these allocates a process secret, two locks and a
bounded semaphore, and an adapter module has to stay importable on a machine with no ComfyUI, no
host directories and no intention of serving a request. It is also the wrong place: the coordinator
must bind onto the same production registry the production route dispatches into, and that ordering
was expressed as a private cross-module import inside a function rather than as wiring anyone could
read.

Nothing here changes what a registry is or what it does. Every one of them already took its ports --
clock, token factory, seed claim, state factory, root factories, artifact inspector -- as
keyword-only constructor arguments with production defaults. What was missing was a supported way to
supply them: a test had to patch a module global, and patching a global is indistinguishable from
the bug where two components disagree about which registry is the live one.

CRITICAL: this module must not import the adapter modules at module level. Each adapter imports this
one, so a module-level import back is a cycle. Construction imports the adapter it is building,
inside `_build`, under the lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any, TypeVar

#: The closed set of process-owned registries. A name that is not here cannot be built, installed or
#: reset, so a new long-lived component has to be declared rather than appearing by assignment.
AUTHORING_WORKSPACE = "authoring_workspace"
EDITOR_RECOVERY = "editor_recovery"
AUTHORING_OUTPUT = "authoring_output"
INPUT_GEOMETRY = "input_geometry"
MANAGED_MODE_QUALIFICATION = "managed_mode_qualification"
MANAGED_SEQUENCE = "managed_sequence"
MEDIA_RUNTIME = "media_runtime"
MEDIA_RUNTIME_RESOLVER = "media_runtime_resolver"
MEDIA_RUNTIME_SETUP = "media_runtime_setup"
PRODUCTION_WORKSPACE = "production_workspace"
PRODUCTION_AUTHORING_IMPORT = "production_authoring_import"
PRODUCTION_PLANNING = "production_planning"
PROJECT_DOCUMENT = "project_document"
PROVIDER_SETTINGS = "provider_settings"
RETAINED_ASSETS = "retained_assets"
SEQUENCE_COORDINATOR = "sequence_coordinator"
SIDEBAR_WORKSPACE = "sidebar_workspace"
WORKSPACE_STATE = "workspace_state"

COMPONENTS = (
    AUTHORING_OUTPUT,
    AUTHORING_WORKSPACE,
    EDITOR_RECOVERY,
    INPUT_GEOMETRY,
    MANAGED_MODE_QUALIFICATION,
    MANAGED_SEQUENCE,
    MEDIA_RUNTIME,
    MEDIA_RUNTIME_RESOLVER,
    MEDIA_RUNTIME_SETUP,
    PRODUCTION_AUTHORING_IMPORT,
    PRODUCTION_WORKSPACE,
    PRODUCTION_PLANNING,
    PROJECT_DOCUMENT,
    PROVIDER_SETTINGS,
    RETAINED_ASSETS,
    SEQUENCE_COORDINATOR,
    SIDEBAR_WORKSPACE,
    WORKSPACE_STATE,
)


T = TypeVar("T")


class CompositionError(RuntimeError):
    """A component was named that this process does not own."""


# CRITICAL: re-entrant, and construction stays inside it. The coordinator's constructor asks for
# the production registry, so a non-re-entrant lock would deadlock on a cold start. Two route
# handlers run on `asyncio.to_thread` worker threads and can both observe an empty table at once;
# since M23-31 the coordinator's constructor binds the process's live-sequence authority onto the
# production registry and binding twice is refused, so an unsynchronised double construction turns
# a cold-start race into a 500 on a request that did nothing wrong. Re-checking inside the lock is
# what makes the binding happen once, and hoisting either check out is the tempting simplification
# that reintroduces it.
_LOCK = RLock()
_INSTANCES: dict[str, Any] = {}


def _build(name: str) -> Any:
    """Select the component's factory and hand it what it needs. Construction lives elsewhere.

    CRITICAL: every branch calls the owning adapter's `build_*` factory rather than its class.
    Constructing inline here would be a second construction site for the same component, and the
    two copies drift silently -- a factory gains an argument, this stays behind, and the process
    runs with the stale construction. That is the same "two components disagree about which
    registry is live" failure this module exists to prevent, so the only thing this function is
    allowed to know is which factory to call and what to pass it.
    """

    if name == AUTHORING_OUTPUT:
        from .comfyui_authoring_output_runtime import build_authoring_output_runtime

        return build_authoring_output_runtime()
    if name == AUTHORING_WORKSPACE:
        from .comfyui_authoring_workspace import build_registry as build_authoring_workspace

        return build_authoring_workspace()
    if name == EDITOR_RECOVERY:
        from .editor_recovery_service import build_editor_recovery_service

        return build_editor_recovery_service(projects=get(PROJECT_DOCUMENT))
    if name == INPUT_GEOMETRY:
        from .comfyui_input_geometry import build_registry as build_input_geometry

        return build_input_geometry()
    if name == MANAGED_MODE_QUALIFICATION:
        from .managed_sequence_service import build_managed_mode_qualification_registry

        return build_managed_mode_qualification_registry(
            production_registry=get(PRODUCTION_WORKSPACE)
        )
    if name == MANAGED_SEQUENCE:
        from .managed_sequence_service import build_managed_sequence_service

        return build_managed_sequence_service(
            production_registry=get(PRODUCTION_WORKSPACE),
            coordinator=get(SEQUENCE_COORDINATOR),
            qualification_guard=get(MANAGED_MODE_QUALIFICATION).guard,
        )
    if name == MEDIA_RUNTIME:
        from .media_runtime_manager import build_media_runtime_manager

        return build_media_runtime_manager(resolver=get(MEDIA_RUNTIME_RESOLVER))
    if name == MEDIA_RUNTIME_RESOLVER:
        from .media_runtime_resolution import build_media_runtime_resolver

        return build_media_runtime_resolver()
    if name == MEDIA_RUNTIME_SETUP:
        from .comfyui_media_runtime_setup import build_media_runtime_setup

        return build_media_runtime_setup(
            resolver=get(MEDIA_RUNTIME_RESOLVER), manager=get(MEDIA_RUNTIME)
        )
    if name == PRODUCTION_WORKSPACE:
        from .comfyui_production_workspace import build_registry as build_production_workspace

        return build_production_workspace()
    if name == PRODUCTION_AUTHORING_IMPORT:
        from .production_authoring_import_service import (
            build_production_authoring_import_service,
        )

        return build_production_authoring_import_service(
            production_registry=get(PRODUCTION_WORKSPACE),
            authoring_registry=get(AUTHORING_WORKSPACE),
        )
    if name == PRODUCTION_PLANNING:
        from .production_planning_service import build_production_planning_service

        return build_production_planning_service(
            sidebar_registry=get(SIDEBAR_WORKSPACE),
            production_registry=get(PRODUCTION_WORKSPACE),
        )
    if name == PROJECT_DOCUMENT:
        from .project_document_service import build_project_document_service

        return build_project_document_service(
            production=get(PRODUCTION_WORKSPACE),
            authoring=get(AUTHORING_WORKSPACE),
            retained=get(RETAINED_ASSETS),
        )
    if name == PROVIDER_SETTINGS:
        from .comfyui_provider_settings import build_registry as build_provider_settings

        return build_provider_settings()
    if name == SEQUENCE_COORDINATOR:
        from .comfyui_sequence_coordinator import build_coordinator

        return build_coordinator(production_registry=get(PRODUCTION_WORKSPACE))
    if name == SIDEBAR_WORKSPACE:
        from .comfyui_sidebar_workspace import build_registry as build_sidebar_workspace

        return build_sidebar_workspace()
    if name == WORKSPACE_STATE:
        from .workspace_state_service import build_workspace_state_service

        return build_workspace_state_service()
    if name == RETAINED_ASSETS:
        from .retained_asset_service import build_retained_asset_service

        return build_retained_asset_service()
    raise CompositionError(f"unknown component: {name}")


def get(name: str) -> Any:
    """The process's one instance of `name`, constructed on first use."""

    if name not in COMPONENTS:
        raise CompositionError(f"unknown component: {name}")
    instance = _INSTANCES.get(name)
    if instance is None:
        with _LOCK:
            instance = _INSTANCES.get(name)
            if instance is None:
                instance = _build(name)
                _INSTANCES[name] = instance
    return instance


def install(name: str, instance: object) -> None:
    """Replace one component for the rest of this process, or until `reset`.

    This is the supported way for a test or a host teardown to supply a registry built with a
    substitute clock, store or id source. It replaces only what it names, so installing one
    component never silently discards another that something else is already holding.
    """

    if name not in COMPONENTS:
        raise CompositionError(f"unknown component: {name}")
    with _LOCK:
        _INSTANCES[name] = instance


def installed(name: str) -> object | None:
    """What is currently installed for `name`, or `None`, without constructing anything.

    CRITICAL: this must not fall through to `get`. Teardown and metadata sampling need only
    existing owners; forcing construction would allocate new live authority during observation.
    """

    if name not in COMPONENTS:
        raise CompositionError(f"unknown component: {name}")
    return _INSTANCES.get(name)


@contextmanager
def substituted(name: str, instance: object) -> Iterator[None]:
    """Install `instance` for the duration of the block, then put back what was there before."""

    previous = installed(name)
    install(name, instance)
    try:
        yield
    finally:
        if previous is None:
            reset(name)
        else:
            install(name, previous)


def reset(*names: str) -> None:
    """Drop the named components, or all of them, so the next `get` builds afresh."""

    selected = names or COMPONENTS
    for name in selected:
        if name not in COMPONENTS:
            raise CompositionError(f"unknown component: {name}")
    with _LOCK:
        for name in selected:
            _INSTANCES.pop(name, None)


def component(name: str, kind: type[T]) -> Callable[[], T]:
    """A typed late-bound accessor for `name`, for a module that wants one at import time.

    CRITICAL: `kind` is checked on every access, not only at construction. `install` accepts any
    object, and the one failure this has to make loud is a substitute that is not the thing it
    replaces -- a stub installed into the process authority would otherwise be discovered as an
    attribute error somewhere far from the install.
    """

    if name not in COMPONENTS:
        raise CompositionError(f"unknown component: {name}")

    def accessor() -> T:
        instance = get(name)
        if not isinstance(instance, kind):
            raise CompositionError(f"{name} is installed as {type(instance).__name__}")
        return instance

    return accessor


__all__ = [
    "AUTHORING_OUTPUT",
    "AUTHORING_WORKSPACE",
    "COMPONENTS",
    "EDITOR_RECOVERY",
    "INPUT_GEOMETRY",
    "MANAGED_MODE_QUALIFICATION",
    "MANAGED_SEQUENCE",
    "MEDIA_RUNTIME",
    "MEDIA_RUNTIME_RESOLVER",
    "MEDIA_RUNTIME_SETUP",
    "PRODUCTION_AUTHORING_IMPORT",
    "PRODUCTION_WORKSPACE",
    "PROVIDER_SETTINGS",
    "RETAINED_ASSETS",
    "SEQUENCE_COORDINATOR",
    "SIDEBAR_WORKSPACE",
    "WORKSPACE_STATE",
    "CompositionError",
    "component",
    "get",
    "install",
    "installed",
    "reset",
    "substituted",
]
