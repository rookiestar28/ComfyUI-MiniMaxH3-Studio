"""The one process-owned media runtime binding every media consumer shares.

M25-30 resolves which qualified ffmpeg/ffprobe pair this host may use and M25-31 can install one.
Neither makes a feature usable: that needs one exact adapter constructed on the admitted pair,
published to the Authoring preview seam and handed to the stateful consumers (final output, M26
assembly stores, derivatives). This manager owns that step, so an absent runtime becomes ready in
the same process after an install or a local selection, without an enable switch, a composition
reset or a host restart.

Construction is inert. The first media request activates the binding; event-loop readers only ask
for a background activation and never wait. A cached binding never authorizes changed bytes: the
adapter still pins each executable on every use.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.canonical import canonical_fingerprint
from .av_reconstruction_media import AVMediaAdapterError, QualifiedAVMediaAdapter
from .composition_root import MEDIA_RUNTIME, component
from .media_runtime_resolution import (
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    ResolutionState,
    SourceKind,
)

FEATURES = ("import", "preview", "derivatives", "assembly", "render")
ACTIVATION_DEADLINE_SECONDS = 60.0
BINDING_SCHEMA = "h3.context.media_runtime_binding.v1"


class MediaRuntimeBusy(RuntimeError):
    """Media work or retained media state blocks a binding transition."""

    code = "media_runtime_busy"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class MediaRuntimeBinding:
    """Host-private. Its repr and every public projection omit locators and identities."""

    adapter: QualifiedAVMediaAdapter
    ffmpeg_path: Path
    ffprobe_path: Path
    scratch_root: Path
    source_kind: SourceKind
    identity: str

    def __repr__(self) -> str:
        return f"<MediaRuntimeBinding source={self.source_kind.value}>"


class BindingConsumer(Protocol):
    """A stateful consumer built on the binding (the final-output runtime)."""

    feature: str

    def attach(self, binding: MediaRuntimeBinding) -> None: ...

    def detach(self) -> None: ...

    def busy(self) -> bool: ...

    def ready(self) -> bool: ...


def binding_identity(resolution: MediaRuntimeResolution) -> str:
    """The identity of a located resolution: its locators and both exact file identities."""

    if resolution.state is not ResolutionState.LOCATED:
        raise ValueError("only a located resolution has a binding identity")
    # IMPORTANT: file identity fields (volume serial, 64-bit file index, mtime in ns) exceed the
    # canonical JSON integer range, so they are fingerprinted as decimal strings. As integers the
    # fingerprint raises for every real file and each activation reports `activation_failed`.
    return canonical_fingerprint(
        {
            "schema": BINDING_SCHEMA,
            "ffmpeg": str(resolution.ffmpeg_path),
            "ffprobe": str(resolution.ffprobe_path),
            "scratch": str(resolution.scratch_root),
            "ffmpeg_identity": [str(value) for value in resolution.ffmpeg_identity or ()],
            "ffprobe_identity": [str(value) for value in resolution.ffprobe_identity or ()],
        }
    )


def _construct_adapter(ffmpeg: Path, ffprobe: Path, scratch: Path) -> QualifiedAVMediaAdapter:
    return QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=scratch,
        clock_ms=lambda: int(time.time() * 1000),
    )


class PreviewPublication:
    """The Authoring preview seam, the one place other readers look up the adapter."""

    def publish(self, adapter: QualifiedAVMediaAdapter) -> None:
        from .comfyui_authoring_media_preview import publish_authoring_media_preview_adapter

        publish_authoring_media_preview_adapter(adapter)

    def clear(self, adapter: QualifiedAVMediaAdapter) -> None:
        from .comfyui_authoring_media_preview import clear_authoring_media_preview_adapter

        clear_authoring_media_preview_adapter(adapter)


class Publisher(Protocol):
    def publish(self, adapter: QualifiedAVMediaAdapter) -> None: ...

    def clear(self, adapter: QualifiedAVMediaAdapter) -> None: ...


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="h3-media-runtime-activation", daemon=True).start()


@dataclass(slots=True)
class _State:
    binding: MediaRuntimeBinding | None = None
    activating: bool = False
    transition: bool = False
    leases: int = 0
    failure: str | None = None
    scoped: dict[str, object] = field(default_factory=dict)


class MediaRuntimeManager:
    """Activation, leases, transitions and feature readiness for the process binding."""

    def __init__(
        self,
        *,
        resolver: MediaRuntimeResolver,
        adapter_factory: Callable[[Path, Path, Path], QualifiedAVMediaAdapter] = (
            _construct_adapter
        ),
        publisher: Publisher | None = None,
        busy_probes: tuple[Callable[[], bool], ...] = (),
        thread_starter: Callable[[Callable[[], None]], None] = _start_daemon,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._adapter_factory = adapter_factory
        self._publisher: Publisher = publisher if publisher is not None else PreviewPublication()
        self._busy_probes = busy_probes
        self._thread_starter = thread_starter
        self._clock = clock
        self._condition = threading.Condition()
        self._build_lock = threading.Lock()
        self._state = _State()
        self._consumers: list[BindingConsumer] = []
        self._stores: dict[str, object] = {}

    def __repr__(self) -> str:
        return "<MediaRuntimeManager>"

    # -- reads -----------------------------------------------------------------------------

    def current(self) -> MediaRuntimeBinding | None:
        """The active binding or `None`.

        CRITICAL: this must stay a lock-and-read. Event-loop handlers (output capability, preview
        admission) call it; resolving, hashing or constructing here would stall the host's event
        loop for as long as discovery and a 0.4 GiB executable hash take.
        """

        with self._condition:
            return self._state.binding

    def ensure(self, *, deadline: float | None = None) -> MediaRuntimeBinding | None:
        """Activate on first use and return the binding. Blocking: worker threads only."""

        limit = self._clock() + ACTIVATION_DEADLINE_SECONDS if deadline is None else deadline
        with self._condition:
            while True:
                state = self._state
                if state.binding is not None:
                    return state.binding
                if not state.activating and not state.transition:
                    state.activating = True
                    break
                remaining = limit - self._clock()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        self._activate_once()
        with self._condition:
            return self._state.binding

    def request_activation(self) -> None:
        """Start one background activation when there is no binding and none is in flight."""

        with self._condition:
            state = self._state
            if state.binding is not None or state.activating or state.transition:
                return
            failure = state.failure
        peeked = self._resolver.peek()
        if peeked is not None and (
            failure is not None
            or peeked.state not in {ResolutionState.LOCATED, ResolutionState.DISCOVERING}
        ):
            # Every poll of an unbound route lands here. An unexpired missing pair or a failed
            # activation of the same resolution would otherwise re-resolve and re-hash both
            # executables per request; it is retried when the resolution expires or on refresh.
            return
        with self._condition:
            state = self._state
            if state.binding is not None or state.activating or state.transition:
                return
            state.activating = True
        try:
            self._thread_starter(self._activate_once)
        except BaseException:
            with self._condition:
                self._state.activating = False
                self._condition.notify_all()
            raise

    # -- leases and transitions ------------------------------------------------------------

    @contextmanager
    def lease(self) -> Iterator[None]:
        """Hold media work against a transition. Refuses instead of queueing behind one."""

        with self._condition:
            if self._state.transition:
                raise MediaRuntimeBusy()
            self._state.leases += 1
        try:
            yield
        finally:
            with self._condition:
                self._state.leases -= 1
                self._condition.notify_all()

    def refresh(
        self,
        *,
        transition: Callable[[], object] | None = None,
        deadline: float | None = None,
    ) -> MediaRuntimeBinding | None:
        """Run a config transition, re-resolve and apply the result under the lease boundary."""

        limit = self._clock() + ACTIVATION_DEADLINE_SECONDS if deadline is None else deadline
        with self._condition:
            while self._state.activating:
                remaining = limit - self._clock()
                if remaining <= 0:
                    raise MediaRuntimeBusy()
                self._condition.wait(remaining)
            # IMPORTANT: busy is decided before the transition runs, so a refused local selection
            # or restore leaves the config bytes exactly as they were. Checking afterwards would
            # persist a selection this process then refuses to apply. Without an active binding
            # there is nothing to discard, so absent-to-ready is never refused as busy.
            if self._state.transition or (self._state.binding is not None and self._busy_locked()):
                raise MediaRuntimeBusy()
            self._state.transition = True
        try:
            if transition is not None:
                transition()
            self._resolver.invalidate()
            resolution = self._resolver.resolve(rescan=True)
            with self._build_lock:
                current = self.current()
                if current is not None and (
                    resolution.state is ResolutionState.DISCOVERING
                    or (
                        resolution.state is ResolutionState.LOCATED
                        and binding_identity(resolution) == current.identity
                    )
                ):
                    # The same pair is a no-op, and a superseded scan proves nothing about it.
                    return current
                if current is not None:
                    self._detach(current)
                return self._apply(resolution)
        finally:
            with self._condition:
                self._state.transition = False
                self._condition.notify_all()

    def _busy_locked(self) -> bool:
        if self._state.leases > 0:
            return True
        for consumer in self._consumers:
            if consumer.busy():
                return True
        for store in self._stores.values():
            if getattr(store, "active_execution_count", 0) > 0:
                return True
        return any(probe() for probe in self._busy_probes)

    # -- consumers and scoped objects ------------------------------------------------------

    def add_consumer(self, consumer: BindingConsumer) -> None:
        with self._condition:
            if consumer in self._consumers:
                return
            self._consumers.append(consumer)
            binding = self._state.binding
        if binding is None:
            return

        def attach_late() -> None:
            # Under the build lock a refresh cannot detach between this check and the attach.
            with self._build_lock:
                with self._condition:
                    if self._state.binding is not binding:
                        return
                consumer.attach(binding)

        # A consumer composed after activation is often composed on the event loop (the first
        # output-capability read); attaching pins both executables, so it runs on a worker.
        self._thread_starter(attach_late)

    def remove_consumer(self, consumer: BindingConsumer) -> None:
        with self._condition:
            if consumer in self._consumers:
                self._consumers.remove(consumer)

    def scoped(self, name: str, factory: Callable[[MediaRuntimeBinding], object]) -> object | None:
        """One object per binding (a derivative generator); dropped when the binding changes."""

        with self._condition:
            binding = self._state.binding
            if binding is None:
                return None
            existing = self._state.scoped.get(name)
        if existing is not None:
            return existing
        # IMPORTANT: factories hash executables or recover a store, so they run under the build
        # lock and never under `_condition`. Building under `_condition` stalls every event-loop
        # `current()` reader for the whole hash.
        with self._build_lock:
            with self._condition:
                if self._state.binding is not binding:
                    return None
                existing = self._state.scoped.get(name)
            if existing is not None:
                return existing
            built = factory(binding)
            with self._condition:
                if self._state.binding is not binding:
                    return None
                self._state.scoped[name] = built
            return built

    def store(self, key: str, factory: Callable[[], object]) -> object:
        """One process object per key (an M26 store root); kept across binding changes."""

        with self._condition:
            existing = self._stores.get(key)
        if existing is not None:
            return existing
        with self._build_lock:
            with self._condition:
                existing = self._stores.get(key)
            if existing is not None:
                return existing
            built = factory()
            with self._condition:
                self._stores[key] = built
            return built

    def stores(self) -> tuple[object, ...]:
        with self._condition:
            return tuple(self._stores.values())

    # -- readiness -------------------------------------------------------------------------

    def readiness(self) -> dict[str, dict[str, str | None]]:
        """Per-feature projection. Non-blocking; asks for activation when there is no binding."""

        binding = self.current()
        if binding is None:
            # IMPORTANT: read the reason before requesting activation. Requesting first reads the
            # manager's own claim back, so an unresolved pair reports "activating" instead of
            # "discovering". `request_activation` decides whether a worker is actually started.
            state, reason = self._unavailable_reason()
            self.request_activation()
            return {feature: {"state": state, "reason": reason} for feature in FEATURES}
        with self._condition:
            consumers = tuple(self._consumers)
        render_ready = any(
            consumer.feature == "render" and consumer.ready() for consumer in consumers
        )
        wire: dict[str, dict[str, str | None]] = {
            feature: {"state": "ready", "reason": None} for feature in FEATURES
        }
        if not render_ready:
            # Installing or locating tools is not renderer proof; only the packaged renderer
            # qualification pinned against this pair is.
            wire["render"] = {"state": "unavailable", "reason": "render_qualification_unavailable"}
        return wire

    def _unavailable_reason(self) -> tuple[str, str]:
        with self._condition:
            activating = self._state.activating or self._state.transition
            failure = self._state.failure
        if activating:
            return "unavailable", "activating"
        if failure is not None:
            return "unavailable", failure
        resolution = self._resolver.peek()
        if resolution is None or resolution.state is ResolutionState.DISCOVERING:
            return "unavailable", "discovering"
        if resolution.state is ResolutionState.LOCATED:
            return "unavailable", "activating"
        if resolution.install_resolves:
            return "setup_required", resolution.reason.value
        if resolution.state is ResolutionState.INVALID_CONFIG:
            return "unavailable", "invalid_config"
        return "unavailable", resolution.reason.value

    # -- activation internals --------------------------------------------------------------

    def _activate_once(self) -> None:
        """Resolve and apply; the caller has already claimed `activating`."""

        try:
            resolution = self._resolver.resolve()
            with self._build_lock:
                self._apply(resolution)
        except Exception:  # noqa: BLE001 - activation failure is a reported state, never a crash
            with self._condition:
                self._state.failure = "activation_failed"
        finally:
            with self._condition:
                self._state.activating = False
                self._condition.notify_all()

    def _apply(self, resolution: MediaRuntimeResolution) -> MediaRuntimeBinding | None:
        if resolution.state is not ResolutionState.LOCATED:
            with self._condition:
                self._state.failure = None
            return None
        ffmpeg, ffprobe, scratch = (
            resolution.ffmpeg_path,
            resolution.ffprobe_path,
            resolution.scratch_root,
        )
        if ffmpeg is None or ffprobe is None or scratch is None:
            raise ValueError("a located resolution carries its pair")
        identity = binding_identity(resolution)
        try:
            adapter = self._adapter_factory(ffmpeg, ffprobe, scratch)
        except (AVMediaAdapterError, OSError, RuntimeError, TypeError, ValueError):
            with self._condition:
                self._state.failure = "activation_failed"
            return None
        binding = MediaRuntimeBinding(
            adapter=adapter,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=scratch,
            source_kind=resolution.source_kind,
            identity=identity,
        )
        try:
            self._publisher.publish(adapter)
        except (RuntimeError, TypeError, ValueError):
            with self._condition:
                self._state.failure = "activation_failed"
            return None
        # CRITICAL: record the binding only after publication succeeded. A binding recorded first
        # would report import/preview ready while the preview seam has no adapter, and the same
        # half-published state would then look like a no-op to every later refresh.
        with self._condition:
            self._state.binding = binding
            self._state.failure = None
            self._state.scoped = {}
            consumers = tuple(self._consumers)
        for consumer in consumers:
            try:
                consumer.attach(binding)
            except Exception:  # noqa: BLE001, S112 - a consumer reports its own readiness
                continue
        return binding

    def _detach(self, binding: MediaRuntimeBinding) -> None:
        with self._condition:
            consumers = tuple(self._consumers)
        for consumer in consumers:
            consumer.detach()
        self._publisher.clear(binding.adapter)
        with self._condition:
            if self._state.binding is binding:
                self._state.binding = None
            self._state.scoped = {}


def _preview_busy() -> bool:
    from .comfyui_authoring_media_preview import authoring_media_preview_busy

    return authoring_media_preview_busy()


def _derivative_busy() -> bool:
    from .comfyui_authoring_media_leases import authoring_media_lease_generation_busy

    return authoring_media_lease_generation_busy()


def build_media_runtime_manager(*, resolver: MediaRuntimeResolver) -> MediaRuntimeManager:
    """Construct the process manager. Called only by the composition root; performs no I/O."""

    return MediaRuntimeManager(resolver=resolver, busy_probes=(_preview_busy, _derivative_busy))


media_runtime_manager = component(MEDIA_RUNTIME, MediaRuntimeManager)


__all__ = [
    "ACTIVATION_DEADLINE_SECONDS",
    "FEATURES",
    "BindingConsumer",
    "MediaRuntimeBinding",
    "MediaRuntimeBusy",
    "MediaRuntimeManager",
    "PreviewPublication",
    "binding_identity",
    "build_media_runtime_manager",
    "media_runtime_manager",
]
