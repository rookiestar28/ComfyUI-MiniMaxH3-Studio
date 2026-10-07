"""Opaque exact-source binding contract for future Authoring preview capture."""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
import time
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, SupportsIndex, runtime_checkable

from ..core.authoring_preview_protocol import (
    AuthoringPreviewCapability,
    AuthoringPreviewCapabilityReason,
)
from ..core.contracts import MediaKind
from ..core.registry import MAX_REFERENCE_IMAGES, MAX_REFERENCE_VIDEOS, ReferenceRegistry
from ..core.safe_paths import UnsafePathError, validate_directory, validate_regular_file
from ..core.timeline_authoring import MAX_REVISION
from .media_preview_authority import (
    MAX_MEDIA_PREVIEW_AUTHORITY_BYTES,
    MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES,
    MAX_MEDIA_PREVIEW_SOURCE_BYTES,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VIDEO_FROM_FILE_BACKING_FIELD = "_VideoFromFile__file"
MAX_PENDING_AUTHORING_SOURCE_BINDINGS = 16
AUTHORING_SOURCE_BINDING_TTL_SECONDS = 15 * 60
MAX_CAPTURE_INPUTS = MAX_REFERENCE_IMAGES + MAX_REFERENCE_VIDEOS + 6
MAX_AUTHORING_SOURCE_DURATION_MILLISECONDS = 30_000

if TYPE_CHECKING:
    from .authoring_image_source import ImageSourcePool, OwnedImageSource


class RuntimeVideoCapability(str, Enum):
    AVAILABLE = "available"
    COMPONENT_BACKED = "component_backed"
    CUSTOM = "custom"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class AuthoringSourceBindingError(RuntimeError):
    """One bounded exact-binding failure with no private runtime detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ComfyVideoInputTypeAuthority:
    """Exact host class objects loaded lazily or supplied by a deterministic test double."""

    video_input: type[object]
    video_from_file: type[object]
    video_from_components: type[object]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.video_input, type)
            or not isinstance(self.video_from_file, type)
            or not isinstance(self.video_from_components, type)
            or self.video_input not in self.video_from_file.__mro__
            or self.video_input not in self.video_from_components.__mro__
            or self.video_from_file is self.video_from_components
        ):
            raise ValueError("Comfy VideoInput type authority is invalid")


def load_comfy_video_input_type_authority() -> ComfyVideoInputTypeAuthority | None:
    """Load the optional public classes only when capability detection is requested."""

    try:
        input_module = import_module("comfy_api.latest._input.video_types")
        implementation_module = import_module("comfy_api.latest._input_impl.video_types")
        video_input = input_module.VideoInput
        video_from_file = implementation_module.VideoFromFile
        video_from_components = implementation_module.VideoFromComponents
        return ComfyVideoInputTypeAuthority(
            video_input=video_input,
            video_from_file=video_from_file,
            video_from_components=video_from_components,
        )
    except (ImportError, AttributeError, ValueError, TypeError):
        return None


class AuthoringSourceBindingReceipt(ABC):
    """Factory-owned receipt whose registry correlation is process-object identity."""

    __slots__ = (
        "_exact_registry",
        "_generation",
        "_released",
        "__weakref__",
        "_lifecycle_lock",
        "_render_borrowers",
        "_render_invalidated",
    )

    def __init__(self, *, exact_registry: ReferenceRegistry, generation: int) -> None:
        if type(exact_registry) is not ReferenceRegistry:
            raise TypeError("exact_registry must be an exact ReferenceRegistry")
        if type(generation) is not int or not 1 <= generation <= MAX_REVISION:
            raise ValueError("generation is invalid")
        self._exact_registry: ReferenceRegistry | None = exact_registry
        self._generation = generation
        self._released = False
        self._lifecycle_lock = threading.RLock()
        self._render_borrowers = 0
        self._render_invalidated: str | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def released(self) -> bool:
        return self._released

    @abstractmethod
    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        """Return the already detected closed capability for one bound source."""

    @abstractmethod
    def _claim_source(self, source_id: str) -> object:
        """Return the exact runtime object after the module guard admits the claim."""

    def _duration_for(self, _source_id: str) -> int | None:
        """Return one content-free probed duration when this receipt owns one."""

        return None

    @abstractmethod
    def _release_sources(self) -> None:
        """Drop every implementation-owned runtime reference; must be idempotent."""

    def release(self) -> None:
        with self._lifecycle_lock:
            if self._released:
                return
            self._released = True
            self._exact_registry = None
            self._release_sources()

    def __repr__(self) -> str:
        return "<AuthoringSourceBindingReceipt opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("authoring source binding receipts are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("authoring source binding receipts are not copyable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("authoring source binding receipts are not serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("authoring source binding receipts are not serializable")


@dataclass(frozen=True, slots=True)
class _RegularFileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


def _identity(metadata: os.stat_result) -> _RegularFileIdentity:
    return _RegularFileIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        modified_ns=int(getattr(metadata, "st_mtime_ns", 0)),
    )


def _admit_path(
    value: str,
    *,
    input_root: Path,
    maximum_bytes: int,
) -> tuple[Path, Path, _RegularFileIdentity]:
    root = validate_directory(input_root)
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    admitted = validate_regular_file(candidate, maximum_bytes=maximum_bytes)
    try:
        admitted.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("regular file is outside the trusted input root") from exc
    metadata = admitted.lstat()
    if int(getattr(metadata, "st_nlink", 1)) != 1:
        raise UnsafePathError("regular file must have exactly one link")
    # IMPORTANT: repeat the shared component validator after containment. A parent or leaf swapped
    # during admission must become unavailable rather than producing a path authority.
    checked = validate_regular_file(admitted, maximum_bytes=maximum_bytes)
    checked_metadata = checked.lstat()
    if _identity(metadata) != _identity(checked_metadata):
        raise UnsafePathError("regular file identity changed during admission")
    return root, checked, _identity(checked_metadata)


class _PathBackedAuthoringVideoSource:
    __slots__ = ("_root", "_path", "_identity", "_maximum_bytes", "_released")

    def __init__(
        self,
        *,
        root: Path,
        path: Path,
        identity: _RegularFileIdentity,
        maximum_bytes: int,
    ) -> None:
        self._root: Path | None = root
        self._path: Path | None = path
        self._identity = identity
        self._maximum_bytes = maximum_bytes
        self._released = False

    def current(self) -> bool:
        if self._released or self._root is None or self._path is None:
            return False
        try:
            _root, _path, identity = _admit_path(
                str(self._path),
                input_root=self._root,
                maximum_bytes=self._maximum_bytes,
            )
        except (OSError, UnsafePathError, ValueError, TypeError):
            return False
        return identity == self._identity

    def claim(self) -> _PathBackedAuthoringVideoSource:
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        return self

    def release(self) -> None:
        self._released = True
        self._root = None
        self._path = None

    def __repr__(self) -> str:
        return "<PathBackedAuthoringVideoSource opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("path-backed authoring sources are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("path-backed authoring sources are not copyable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("path-backed authoring sources are not serializable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("path-backed authoring sources are not serializable")


class _PathBackedReceipt(AuthoringSourceBindingReceipt):
    __slots__ = ("_capabilities", "_durations", "_sources")

    def __init__(
        self,
        *,
        exact_registry: ReferenceRegistry,
        generation: int,
        capabilities: dict[str, RuntimeVideoCapability],
        sources: dict[str, _PathBackedAuthoringVideoSource],
        durations: dict[str, int],
    ) -> None:
        super().__init__(exact_registry=exact_registry, generation=generation)
        self._capabilities = capabilities
        self._sources = sources
        self._durations = durations

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        try:
            capability = self._capabilities[source_id]
        except KeyError as exc:
            raise AuthoringSourceBindingError("source_not_found") from exc
        if capability is RuntimeVideoCapability.AVAILABLE:
            source = self._sources.get(source_id)
            if source is None or not source.current():
                return RuntimeVideoCapability.STALE
        return capability

    def _claim_source(self, source_id: str) -> object:
        try:
            return self._sources[source_id].claim()
        except KeyError as exc:
            raise AuthoringSourceBindingError("source_not_found") from exc

    def _duration_for(self, source_id: str) -> int | None:
        if self._capability_for(source_id) is not RuntimeVideoCapability.AVAILABLE:
            return None
        return self._durations.get(source_id)

    def _release_sources(self) -> None:
        for source in self._sources.values():
            source.release()
        self._sources.clear()
        self._capabilities.clear()
        self._durations.clear()


def _probe_path_backed_authoring_duration(source: object) -> int | None:
    """Use only the explicitly published exact adapter; never discover a runtime or locator."""

    from .av_reconstruction_media import AVMediaAdapterError, QualifiedAVMediaAdapter
    from .comfyui_authoring_media_preview import current_authoring_media_preview_adapter
    from .comfyui_media_runtime import media_runtime_lease

    if type(source) is not _PathBackedAuthoringVideoSource:
        return None
    try:
        # A busy transition (`MediaRuntimeBusy` is a RuntimeError) leaves the duration unknown.
        with media_runtime_lease():
            adapter = current_authoring_media_preview_adapter()
            if (
                type(adapter) is not QualifiedAVMediaAdapter
                or not source.current()
                or source._path is None
            ):
                return None
            duration = adapter.probe_authoring_source_duration(
                source_path=source._path,
                deadline=time.monotonic() + 30.0,
            )
    except (AVMediaAdapterError, OSError, RuntimeError, TypeError, ValueError):
        return None
    # CRITICAL: duration may authorize timed-source admission. Revalidate the exact file after the
    # subprocess window; otherwise a replaced source could borrow the probed file's authority.
    if not source.current():
        return None
    return duration


def _host_input_root() -> Path:
    module = sys.modules.get("folder_paths")
    factory = getattr(module, "get_input_directory", None)
    if not callable(factory):
        raise RuntimeError("host input root is unavailable")
    value = factory()
    if type(value) is not str or not value:
        raise RuntimeError("host input root is unavailable")
    return Path(value)


class PathBackedComfyVideoFromFileV1Factory:
    """Create opaque path authorities from the one pinned, method-free ComfyUI source shape."""

    def __init__(
        self,
        *,
        type_authority: ComfyVideoInputTypeAuthority | None = None,
        input_root_factory: Callable[[], Path] = _host_input_root,
        max_source_bytes: int = MAX_MEDIA_PREVIEW_SOURCE_BYTES,
        max_authority_bytes: int = MAX_MEDIA_PREVIEW_AUTHORITY_BYTES,
        max_authority_set_bytes: int = MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES,
        duration_probe: Callable[[object], int | None] | None = None,
    ) -> None:
        if type_authority is not None and not isinstance(
            type_authority, ComfyVideoInputTypeAuthority
        ):
            raise TypeError("type authority is invalid")
        if not callable(input_root_factory):
            raise TypeError("input root factory is invalid")
        if duration_probe is not None and not callable(duration_probe):
            raise TypeError("duration probe is invalid")
        if (
            type(max_source_bytes) is not int
            or not 1 <= max_source_bytes <= MAX_MEDIA_PREVIEW_SOURCE_BYTES
        ):
            raise ValueError("source byte limit is invalid")
        if (
            type(max_authority_bytes) is not int
            or not 1 <= max_authority_bytes <= MAX_MEDIA_PREVIEW_AUTHORITY_BYTES
            or type(max_authority_set_bytes) is not int
            or not 1 <= max_authority_set_bytes <= MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES
        ):
            raise ValueError("authority byte limit is invalid")
        self._type_authority = type_authority
        self._input_root_factory = input_root_factory
        self._max_source_bytes = max_source_bytes
        self._max_authority_bytes = max_authority_bytes
        self._max_authority_set_bytes = max_authority_set_bytes
        self._duration_probe = duration_probe or _probe_path_backed_authoring_duration

    def capture(
        self,
        *,
        exact_registry: ReferenceRegistry,
        generation: int,
        sources: tuple[tuple[str, MediaKind, object], ...],
    ) -> AuthoringSourceBindingReceipt:
        if type(exact_registry) is not ReferenceRegistry:
            raise AuthoringSourceBindingError("registry_mismatch")
        if type(sources) is not tuple or len(sources) > MAX_CAPTURE_INPUTS:
            raise AuthoringSourceBindingError("source_count_exceeded")
        registry_assets = {asset.asset_id: asset for asset in exact_registry.assets}
        capabilities = {
            asset.asset_id: RuntimeVideoCapability.UNSUPPORTED for asset in exact_registry.assets
        }
        admitted_sources: dict[str, _PathBackedAuthoringVideoSource] = {}
        durations: dict[str, int] = {}
        seen: set[str] = set()
        aggregate_authority_bytes = 0
        authority = self._type_authority or load_comfy_video_input_type_authority()

        for item in sources:
            if type(item) is not tuple or len(item) != 3:
                raise AuthoringSourceBindingError("source_mismatch")
            source_id, kind, value = item
            asset = registry_assets.get(source_id) if type(source_id) is str else None
            if (
                asset is None
                or source_id in seen
                or not isinstance(kind, MediaKind)
                or asset.kind is not kind
            ):
                for source in admitted_sources.values():
                    source.release()
                raise AuthoringSourceBindingError("source_mismatch")
            seen.add(source_id)
            # CRITICAL: AUDIO and non-VIDEO runtime objects must leave this loop without entering
            # any receipt-owned container; retaining them would create an unauthorized source seam.
            if kind is not MediaKind.VIDEO:
                continue
            detected = detect_comfy_video_input_capability(value, type_authority=authority)
            if detected is not RuntimeVideoCapability.AVAILABLE:
                capabilities[source_id] = detected
                continue
            try:
                # IMPORTANT: the pinned constructor field is the only method-free inspection seam.
                # Calling get_stream_source() can seek BytesIO and breaks the no-I/O contract.
                backing = object.__getattribute__(value, _VIDEO_FROM_FILE_BACKING_FIELD)
            except (AttributeError, TypeError):
                capabilities[source_id] = RuntimeVideoCapability.UNSUPPORTED
                continue
            if type(backing) is not str:
                capabilities[source_id] = RuntimeVideoCapability.UNSUPPORTED
                continue
            try:
                root = self._input_root_factory()
                if not isinstance(root, Path):
                    raise TypeError("input root must be pathlib.Path")
                root, path, file_identity = _admit_path(
                    backing,
                    input_root=root,
                    maximum_bytes=self._max_source_bytes,
                )
                authority_bytes = len(os.fsencode(str(path))) + len(source_id.encode("utf-8")) + 128
                if authority_bytes > self._max_authority_bytes:
                    raise ValueError("authority bytes exceeded")
                aggregate_authority_bytes += authority_bytes
                if aggregate_authority_bytes > self._max_authority_set_bytes:
                    raise ValueError("authority set bytes exceeded")
            except (OSError, UnsafePathError, ValueError, TypeError, RuntimeError):
                capabilities[source_id] = RuntimeVideoCapability.UNAVAILABLE
                continue
            source = _PathBackedAuthoringVideoSource(
                root=root,
                path=path,
                identity=file_identity,
                maximum_bytes=self._max_source_bytes,
            )
            admitted_sources[source_id] = source
            capabilities[source_id] = RuntimeVideoCapability.AVAILABLE
            try:
                duration = self._duration_probe(source)
            except Exception:  # noqa: BLE001 - an optional probe can never break source capture.
                duration = None
            if (
                type(duration) is int
                and 1 <= duration <= MAX_AUTHORING_SOURCE_DURATION_MILLISECONDS
                and source.current()
            ):
                durations[source_id] = duration

        return _PathBackedReceipt(
            exact_registry=exact_registry,
            generation=generation,
            capabilities=capabilities,
            sources=admitted_sources,
            durations=durations,
        )


class _RuntimeReceipt(AuthoringSourceBindingReceipt):
    def __init__(
        self,
        video: AuthoringSourceBindingReceipt,
        images: dict[str, OwnedImageSource],
        capabilities: dict[str, RuntimeVideoCapability],
    ) -> None:
        if video._exact_registry is None:
            raise AuthoringSourceBindingError("receipt_released")
        super().__init__(exact_registry=video._exact_registry, generation=video.generation)
        self._video = video
        self._images = images
        self._image_capabilities = capabilities

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        if source_id not in self._image_capabilities:
            return self._video._capability_for(source_id)
        capability = self._image_capabilities[source_id]
        if capability is RuntimeVideoCapability.AVAILABLE:
            image = self._images.get(source_id)
            if image is None or not image.current():
                return RuntimeVideoCapability.STALE
        return capability

    def _claim_source(self, source_id: str) -> object:
        if source_id in self._images:
            return self._images[source_id]
        return self._video._claim_source(source_id)

    def _duration_for(self, source_id: str) -> int | None:
        return self._video._duration_for(source_id)

    def _release_sources(self) -> None:
        self._video.release()
        for image in self._images.values():
            image.release()
        self._images.clear()
        self._image_capabilities.clear()


class RuntimeComfySourceFactory:
    """The registered Context origins: exact file-backed VIDEO and captured CPU IMAGE."""

    def __init__(
        self,
        *,
        image_pool: ImageSourcePool | None = None,
        video_factory: PathBackedComfyVideoFromFileV1Factory | None = None,
    ) -> None:
        self._images = image_pool
        self._image_pool_lock = threading.Lock()
        self._video = video_factory or PathBackedComfyVideoFromFileV1Factory()

    def _image_pool(self) -> ImageSourcePool:
        # CRITICAL: keep the IMAGE module/runtime lazy. Eager construction at the process binding
        # footer creates a circular import when the image authority itself is the entry module.
        from .authoring_image_source import ImageSourcePool

        with self._image_pool_lock:
            if self._images is None:
                self._images = ImageSourcePool()
            return self._images

    def capture(
        self,
        *,
        exact_registry: ReferenceRegistry,
        generation: int,
        sources: tuple[tuple[str, MediaKind, object], ...],
    ) -> AuthoringSourceBindingReceipt:
        # The VIDEO factory validates the complete ID/kind/duplicate contract first. Standalone
        # AUDIO remains unsupported and never enters a receipt-owned container.
        video = self._video.capture(
            exact_registry=exact_registry,
            generation=generation,
            sources=sources,
        )
        images: dict[str, OwnedImageSource] = {}
        capabilities: dict[str, RuntimeVideoCapability] = {}
        try:
            for source_id, kind, value in sources:
                if kind is not MediaKind.IMAGE:
                    continue
                try:
                    images[source_id] = self._image_pool().capture(value)
                    capabilities[source_id] = RuntimeVideoCapability.AVAILABLE
                except AuthoringSourceBindingError as exc:
                    capabilities[source_id] = (
                        RuntimeVideoCapability.UNSUPPORTED
                        if exc.code == "source_unsupported"
                        else RuntimeVideoCapability.UNAVAILABLE
                    )
            return _RuntimeReceipt(video, images, capabilities)
        except BaseException:
            video.release()
            for image in images.values():
                image.release()
            raise


@dataclass(slots=True)
class _PendingReceipt:
    receipt: AuthoringSourceBindingReceipt
    touched_at: float


class ProcessLocalAuthoringSourceBindingStore:
    """Finite pending-receipt holder keyed only by exact process registry identity."""

    def __init__(
        self,
        *,
        factory: AuthoringSourceBindingFactory,
        max_entries: int = MAX_PENDING_AUTHORING_SOURCE_BINDINGS,
        ttl_seconds: int = AUTHORING_SOURCE_BINDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(factory, AuthoringSourceBindingFactory):
            raise TypeError("source binding factory is invalid")
        if (
            type(max_entries) is not int
            or not 1 <= max_entries <= MAX_PENDING_AUTHORING_SOURCE_BINDINGS
        ):
            raise ValueError("pending source binding limit is invalid")
        if (
            type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= AUTHORING_SOURCE_BINDING_TTL_SECONDS
        ):
            raise ValueError("pending source binding TTL is invalid")
        if not callable(clock):
            raise TypeError("clock is invalid")
        self._factory = factory
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._generation = 0
        self._entries: OrderedDict[int, _PendingReceipt] = OrderedDict()
        self._live: weakref.WeakValueDictionary[int, AuthoringSourceBindingReceipt] = (
            weakref.WeakValueDictionary()
        )
        self._lock = threading.RLock()
        self._capture_gate = threading.Lock()
        self._closed = False

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.touched_at >= self._ttl_seconds
        ]
        for key in expired:
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.receipt.release()
        while len(self._entries) > self._max_entries:
            _key, entry = self._entries.popitem(last=False)
            entry.receipt.release()

    def capture(
        self,
        *,
        exact_registry: ReferenceRegistry,
        sources: tuple[tuple[str, MediaKind, object], ...],
    ) -> AuthoringSourceBindingReceipt:
        if not self._capture_gate.acquire(blocking=False):
            raise AuthoringSourceBindingError("source_capture_busy")
        receipt: AuthoringSourceBindingReceipt | None = None
        try:
            with self._lock:
                if self._closed:
                    raise AuthoringSourceBindingError("store_closed")
                now = self._clock()
                self._prune(now)
                for key, prior in tuple(self._live.items()):
                    with prior._lifecycle_lock:
                        # CRITICAL: workspace release is not invocation replacement. Keep a
                        # running job weakly observable so a later capture can revoke its bytes.
                        if prior.released and not prior._render_borrowers:
                            self._live.pop(key, None)
                key = id(exact_registry)
                if (
                    key not in self._live
                    and len(self._live) >= 2 * MAX_PENDING_AUTHORING_SOURCE_BINDINGS
                ):
                    raise AuthoringSourceBindingError("source_count_exceeded")
                if self._generation >= MAX_REVISION:
                    raise AuthoringSourceBindingError("generation_exhausted")
                self._generation += 1
                generation = self._generation
            receipt = self._factory.capture(
                exact_registry=exact_registry,
                generation=generation,
                sources=sources,
            )
            with self._lock:
                if self._closed:
                    raise AuthoringSourceBindingError("store_closed")
                # CRITICAL: transfer is not permission to keep an earlier invocation current.
                # Track live receipts weakly so replacement invalidates workspace-owned copies
                # without retaining private sources after their owning workspace releases them.
                replaced = self._live.get(key)
                if replaced is not None:
                    with replaced._lifecycle_lock:
                        replaced._render_invalidated = "source_replaced"
                        replaced.release()
                self._entries.pop(key, None)
                self._live[key] = receipt
                self._entries[key] = _PendingReceipt(receipt, now)
                self._prune(self._clock())
                return receipt
        except BaseException:
            if receipt is not None:
                receipt.release()
            raise
        finally:
            self._capture_gate.release()

    def claim(self, exact_registry: ReferenceRegistry) -> AuthoringSourceBindingReceipt | None:
        if type(exact_registry) is not ReferenceRegistry:
            return None
        now = self._clock()
        with self._lock:
            if self._closed:
                return None
            self._prune(now)
            entry = self._entries.pop(id(exact_registry), None)
            if entry is None:
                return None
            if entry.receipt._exact_registry is not exact_registry:
                entry.receipt.release()
                return None
            return entry.receipt

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for receipt in tuple(self._live.values()):
                with receipt._lifecycle_lock:
                    if receipt._render_invalidated is None:
                        receipt._render_invalidated = "service_closed"
            for entry in self._entries.values():
                entry.receipt.release()
            self._entries.clear()


@runtime_checkable
class AuthoringSourceBindingFactory(Protocol):
    """M25-01 implementation seam; capture occurs beside the successful node return."""

    def capture(
        self,
        *,
        exact_registry: ReferenceRegistry,
        generation: int,
        sources: tuple[tuple[str, MediaKind, object], ...],
    ) -> AuthoringSourceBindingReceipt: ...


def detect_comfy_video_input_capability(
    value: object,
    *,
    type_authority: ComfyVideoInputTypeAuthority | None = None,
) -> RuntimeVideoCapability:
    """Classify the pinned public VideoInput shapes without importing or invoking ComfyUI."""

    authority = type_authority or load_comfy_video_input_type_authority()
    if authority is None:
        return RuntimeVideoCapability.UNSUPPORTED
    value_type = type(value)
    # IMPORTANT: never duck-type by calling get_stream_source/get_components. A custom method may
    # decode an unbounded tensor or perform I/O merely during capability discovery.
    if authority.video_input not in value_type.__mro__:
        return RuntimeVideoCapability.UNSUPPORTED
    if value_type is authority.video_from_file:
        return RuntimeVideoCapability.AVAILABLE
    if value_type is authority.video_from_components:
        return RuntimeVideoCapability.COMPONENT_BACKED
    return RuntimeVideoCapability.CUSTOM


def claim_exact_authoring_source(
    receipt: AuthoringSourceBindingReceipt,
    *,
    exact_registry: ReferenceRegistry,
    expected_generation: int,
    source_id: str,
) -> object:
    """Claim only the exact registry object/generation; equality and fingerprints never qualify."""

    if not isinstance(receipt, AuthoringSourceBindingReceipt):
        raise AuthoringSourceBindingError("invalid_receipt")
    if receipt._released:
        raise AuthoringSourceBindingError("receipt_released")
    if (
        type(exact_registry) is not ReferenceRegistry
        or receipt._exact_registry is not exact_registry
    ):
        raise AuthoringSourceBindingError("registry_mismatch")
    if type(expected_generation) is not int or expected_generation != receipt._generation:
        raise AuthoringSourceBindingError("generation_mismatch")
    if type(source_id) is not str or _IDENTIFIER.fullmatch(source_id) is None:
        raise AuthoringSourceBindingError("source_id_invalid")
    try:
        capability = receipt._capability_for(source_id)
    except AuthoringSourceBindingError:
        raise
    except Exception as exc:
        raise AuthoringSourceBindingError("source_not_found") from exc
    if capability is not RuntimeVideoCapability.AVAILABLE:
        if capability is RuntimeVideoCapability.STALE:
            raise AuthoringSourceBindingError("source_stale")
        raise AuthoringSourceBindingError("source_unsupported")
    try:
        return receipt._claim_source(source_id)
    except AuthoringSourceBindingError:
        raise
    except Exception as exc:
        raise AuthoringSourceBindingError("source_not_found") from exc


def authoring_source_preview_capability(
    receipt: AuthoringSourceBindingReceipt | None,
    source_id: str,
) -> AuthoringPreviewCapability:
    """Project one closed content-free capability without returning private authority detail."""

    if receipt is None:
        return AuthoringPreviewCapability(
            available=False,
            reason=AuthoringPreviewCapabilityReason.NOT_BOUND,
        )
    if not isinstance(receipt, AuthoringSourceBindingReceipt) or receipt.released:
        return AuthoringPreviewCapability(
            available=False,
            reason=AuthoringPreviewCapabilityReason.EXPIRED,
        )
    if type(source_id) is not str or _IDENTIFIER.fullmatch(source_id) is None:
        return AuthoringPreviewCapability(
            available=False,
            reason=AuthoringPreviewCapabilityReason.UNAVAILABLE,
        )
    try:
        capability = receipt._capability_for(source_id)
    except AuthoringSourceBindingError:
        return AuthoringPreviewCapability(
            available=False,
            reason=AuthoringPreviewCapabilityReason.NOT_BOUND,
        )
    if capability is RuntimeVideoCapability.AVAILABLE:
        registry = receipt._exact_registry
        if registry is not None and any(
            asset.asset_id == source_id and asset.kind is not MediaKind.VIDEO
            for asset in registry.assets
        ):
            # IMAGE ownership qualifies render preparation only; the existing preview executor
            # is VIDEO-only and must not advertise an unexecuted image-preview control.
            return AuthoringPreviewCapability(
                available=False,
                reason=AuthoringPreviewCapabilityReason.UNSUPPORTED,
            )
        return AuthoringPreviewCapability(available=True)
    if capability is RuntimeVideoCapability.STALE:
        reason = AuthoringPreviewCapabilityReason.STALE
    elif capability is RuntimeVideoCapability.UNAVAILABLE:
        reason = AuthoringPreviewCapabilityReason.UNAVAILABLE
    else:
        reason = AuthoringPreviewCapabilityReason.UNSUPPORTED
    return AuthoringPreviewCapability(available=False, reason=reason)


def authoring_source_duration_milliseconds(
    receipt: AuthoringSourceBindingReceipt | None,
    source_id: str,
) -> int | None:
    """Return only a bounded content-free duration from one live opaque receipt."""

    if (
        not isinstance(receipt, AuthoringSourceBindingReceipt)
        or receipt.released
        or type(source_id) is not str
        or _IDENTIFIER.fullmatch(source_id) is None
    ):
        return None
    try:
        duration = receipt._duration_for(source_id)
    except (AuthoringSourceBindingError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if type(duration) is not int or not 1 <= duration <= MAX_AUTHORING_SOURCE_DURATION_MILLISECONDS:
        return None
    return duration


def claim_transferred_authoring_source(
    receipt: AuthoringSourceBindingReceipt,
    source_id: str,
) -> object:
    """Claim from an already exact-correlated receipt after its one-way workspace transfer."""

    if not isinstance(receipt, AuthoringSourceBindingReceipt) or receipt._exact_registry is None:
        raise AuthoringSourceBindingError("invalid_receipt")
    # CRITICAL: use the receipt-owned exact registry and generation. Accepting a reconstructed
    # registry here would reopen the fingerprint-only wrong-file claim M25-01 closed.
    return claim_exact_authoring_source(
        receipt,
        exact_registry=receipt._exact_registry,
        expected_generation=receipt._generation,
        source_id=source_id,
    )


def execute_transferred_authoring_preview(
    source: object,
    adapter: object,
    *,
    source_start_frame: int,
    frames: int,
    source_fps: int,
    deadline: float,
    cancellation: object | None = None,
) -> tuple[bytearray, str]:
    """Keep the private locator inside its authority module while invoking qualification."""

    from .av_reconstruction_media import QualifiedAVMediaAdapter

    if type(adapter) is not QualifiedAVMediaAdapter:
        raise AuthoringSourceBindingError("adapter_unavailable")
    if type(source) is _PathBackedAuthoringVideoSource:
        source_path = source._path
    else:
        # Imported artifacts have a distinct owner type and never pass through input-root
        # admission.  Keep the private locator inside its owning module.
        from .authoring_generated_source import GeneratedAuthoringVideoSource
        from .retained_asset_use import RetainedVideoSource

        if type(source) is GeneratedAuthoringVideoSource:
            source_path = source.lease.path
        elif type(source) is RetainedVideoSource:
            if not source.current():
                raise AuthoringSourceBindingError("source_stale")
            source_path = source.lease.path
        else:
            raise AuthoringSourceBindingError("source_unsupported")
    if not source.current() or source_path is None:
        raise AuthoringSourceBindingError("source_stale")
    body, disposition = adapter.execute_authoring_preview(
        source_path=source_path,
        source_start_frame=source_start_frame,
        frames=frames,
        source_fps=source_fps,
        deadline=deadline,
        cancellation=cancellation,  # type: ignore[arg-type]
    )
    # IMPORTANT: replacement during the subprocess window invalidates the whole derivative;
    # returning the old body would break the workspace/source late-currentness contract.
    if not source.current():
        body.clear()
        raise AuthoringSourceBindingError("source_stale")
    return body, disposition


_PROCESS_BINDINGS = ProcessLocalAuthoringSourceBindingStore(factory=RuntimeComfySourceFactory())


def capture_process_authoring_sources(
    *,
    exact_registry: ReferenceRegistry,
    sources: tuple[tuple[str, MediaKind, object], ...],
) -> AuthoringSourceBindingReceipt:
    return _PROCESS_BINDINGS.capture(exact_registry=exact_registry, sources=sources)


def claim_process_authoring_sources(
    exact_registry: ReferenceRegistry,
) -> AuthoringSourceBindingReceipt | None:
    return _PROCESS_BINDINGS.claim(exact_registry)


atexit.register(_PROCESS_BINDINGS.close)


__all__ = [
    "AUTHORING_SOURCE_BINDING_TTL_SECONDS",
    "AuthoringSourceBindingError",
    "AuthoringSourceBindingFactory",
    "AuthoringSourceBindingReceipt",
    "ComfyVideoInputTypeAuthority",
    "MAX_PENDING_AUTHORING_SOURCE_BINDINGS",
    "MAX_AUTHORING_SOURCE_DURATION_MILLISECONDS",
    "PathBackedComfyVideoFromFileV1Factory",
    "ProcessLocalAuthoringSourceBindingStore",
    "RuntimeVideoCapability",
    "authoring_source_preview_capability",
    "authoring_source_duration_milliseconds",
    "capture_process_authoring_sources",
    "claim_exact_authoring_source",
    "claim_process_authoring_sources",
    "claim_transferred_authoring_source",
    "detect_comfy_video_input_capability",
    "execute_transferred_authoring_preview",
    "load_comfy_video_input_type_authority",
]
