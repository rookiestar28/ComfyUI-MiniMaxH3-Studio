"""One process-local final-output runtime attached to the shared media runtime binding."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.authoring_output_protocol import output_capability_wire
from .authoring_native_renderer import NativeAuthoringRenderer
from .authoring_output_preview import NativeOutputPreview
from .authoring_output_service import AuthoringOutputRegistry
from .authoring_render_process import pin_render_executable
from .authoring_render_service import AuthoringRenderService
from .authoring_render_store import RenderOutputStore
from .authoring_renderer_qualification import load_renderer_qualification
from .comfyui_authoring_output import AuthoringOutputRoutes
from .comfyui_authoring_workspace import AuthoringWorkspaceRegistry
from .comfyui_route_seam import host_web_and_routes
from .composition_root import AUTHORING_OUTPUT, AUTHORING_WORKSPACE, MEDIA_RUNTIME, component, get

if TYPE_CHECKING:
    from .media_runtime_manager import MediaRuntimeBinding, MediaRuntimeManager


@dataclass(frozen=True, slots=True)
class _PinControl:
    deadline: float

    def is_cancelled(self) -> bool:
        return False


def _build_registry(
    binding: MediaRuntimeBinding, workspace: AuthoringWorkspaceRegistry | None
) -> AuthoringOutputRegistry | None:
    """The qualified output registry on the binding's pair, or `None` when it does not qualify."""

    if sys.platform != "win32":
        return None
    store = None
    preview = None
    service = None
    try:
        identity = load_renderer_qualification()
        control = _PinControl(time.monotonic() + 30)
        with (
            pin_render_executable(
                binding.ffmpeg_path, identity.renderer_fingerprint, control=control
            ),
            pin_render_executable(
                binding.ffprobe_path, identity.probe_fingerprint, control=control
            ),
        ):
            owner = workspace or component(AUTHORING_WORKSPACE, AuthoringWorkspaceRegistry)()
            store = RenderOutputStore(binding.scratch_root)
            preview = NativeOutputPreview(
                root=binding.scratch_root,
                renderer_path=binding.ffmpeg_path,
                probe_path=binding.ffprobe_path,
            )
            backend = NativeAuthoringRenderer(
                renderer_path=binding.ffmpeg_path, probe_path=binding.ffprobe_path
            )
            service = AuthoringRenderService(store, backend=backend)
            return AuthoringOutputRegistry(
                workspace=owner, service=service, store=store, preview=preview
            )
    except Exception:
        # A pair that does not match the packaged renderer qualification is an unavailable render
        # capability, never a fallback and never a reason to fail the other media features.
        if service is not None:
            service.close()
        elif store is not None:
            store.close()
        if preview is not None:
            preview.close()
        return None


class AuthoringOutputRuntime:
    """Own transport workers and the optional qualified service, with explicit shutdown."""

    feature = "render"

    def __init__(
        self,
        registry: AuthoringOutputRegistry | None,
        *,
        workspace: AuthoringWorkspaceRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._workspace = workspace
        self._lock = threading.Lock()
        self._manager: MediaRuntimeManager | None = None
        self.routes = AuthoringOutputRoutes(self._route_registry)

    def __repr__(self) -> str:
        return "<AuthoringOutputRuntime opaque>"

    def registry(self) -> AuthoringOutputRegistry | None:
        with self._lock:
            return self._registry

    def _route_registry(self) -> AuthoringOutputRegistry | None:
        registry = self.registry()
        manager = self._manager
        if registry is None and manager is not None:
            # Route handlers run on the event loop: ask for activation, never wait for it.
            manager.request_activation()
        return registry

    def capability(self) -> dict[str, object]:
        return output_capability_wire(supported=self.registry() is not None)

    # -- media runtime consumer ------------------------------------------------------------

    def attach(self, binding: MediaRuntimeBinding) -> None:
        """Build the qualified registry on a new binding. Runs on the activating worker thread."""

        with self._lock:
            if self._registry is not None:
                return
        registry = _build_registry(binding, self._workspace)
        with self._lock:
            if self._registry is None:
                self._registry, registry = registry, None
        if registry is not None:
            registry.close()

    def detach(self) -> None:
        with self._lock:
            registry, self._registry = self._registry, None
        if registry is not None:
            registry.close()

    def busy(self) -> bool:
        registry = self.registry()
        return registry is not None and registry.busy

    def ready(self) -> bool:
        return self.registry() is not None

    def close(self) -> None:
        with self._lock:
            registry, self._registry = self._registry, None
        # IMPORTANT: signal registry/child shutdown before waiting on transport workers.
        # Reversing this order makes an abandoned preview keep shutdown waiting for its deadline.
        try:
            if registry is not None:
                registry.close()
        finally:
            self.routes.close()


def build_authoring_output_runtime(
    *,
    workspace: AuthoringWorkspaceRegistry | None = None,
) -> AuthoringOutputRuntime:
    """An output runtime with no registry, attached to the process media runtime manager.

    CRITICAL: no environment reader and no registry built here. The M25-22 runtime read the
    environment once at construction and cached an absent registry for the life of the process, so
    an install or a local selection could never enable final output without a restart. The manager
    now calls `attach` when a binding becomes active, on its worker thread.
    """

    from .media_runtime_manager import MediaRuntimeManager

    runtime = AuthoringOutputRuntime(None, workspace=workspace)
    manager = get(MEDIA_RUNTIME)
    if isinstance(manager, MediaRuntimeManager):
        runtime._manager = manager
        manager.add_consumer(runtime)
    return runtime


_runtime = component(AUTHORING_OUTPUT, AuthoringOutputRuntime)


def authoring_output_capability() -> dict[str, object]:
    return _runtime().capability()


def ensure_authoring_output_route_registered() -> bool:
    found = host_web_and_routes()
    if found is None:
        return False
    web, routes = found
    return _runtime().routes.register(web, routes)
