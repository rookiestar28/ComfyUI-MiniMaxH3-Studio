"""Bounded, CPU-owned IMAGE snapshots captured beside the exact Context invocation."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from importlib import import_module
from typing import Any, NoReturn, SupportsIndex, cast

from .authoring_source_binding import AuthoringSourceBindingError

IMAGE_SOURCE_PROFILE = "h3.authoring.image.cpu_float32_rgb.v1"
MAX_IMAGE_SOURCE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_POOL_BYTES = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_194_304
MAX_IMAGE_EDGE = 8192
MAX_IMAGE_SOURCES = 16 * 11


class OwnedImageSource:
    """Private immutable pixels; access never returns the caller's mutable tensor."""

    __slots__ = ("_pool", "_pixels", "_expires", "_width", "_height", "_fingerprint")

    def __init__(
        self,
        pool: ImageSourcePool,
        pixels: bytes,
        width: int,
        height: int,
        expires: float,
    ) -> None:
        self._pool = pool
        self._pixels: bytes | None = pixels
        self._expires = expires
        self._width = width
        self._height = height
        digest = hashlib.sha256()
        digest.update(f"{IMAGE_SOURCE_PROFILE}:{width}:{height}:".encode("ascii"))
        digest.update(pixels)
        self._fingerprint = digest.hexdigest()

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def current(self) -> bool:
        with self._pool._lock:
            self._pool._prune_locked()
            return self._pixels is not None and not self._pool._closed

    def read_bytes(self) -> bytes:
        with self._pool._lock:
            if not self.current() or self._pixels is None:
                raise AuthoringSourceBindingError("source_stale")
            return self._pixels

    def release(self) -> None:
        with self._pool._lock:
            self._pool._release_locked(self)

    def __repr__(self) -> str:
        return "<OwnedImageSource opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("image source leases are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("image source leases are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("image source leases are not serializable")


class ImageSourcePool:
    """Finite process ownership including provisional copy and validation scratch."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_IMAGE_POOL_BYTES,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: int = 900,
    ) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_IMAGE_POOL_BYTES:
            raise ValueError("image pool byte limit is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900 or not callable(clock):
            raise ValueError("image pool lifetime is invalid")
        self._max_bytes = max_bytes
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._copy_gate = threading.Lock()
        self._sources: dict[int, OwnedImageSource] = {}
        self._live_bytes = 0
        self._reserved_bytes = 0
        self._closed = False

    @property
    def live_bytes(self) -> int:
        with self._lock:
            self._prune_locked()
            return self._live_bytes

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    def _release_locked(self, source: OwnedImageSource) -> None:
        if source._pixels is not None:
            self._live_bytes -= len(source._pixels)
            source._pixels = None
        self._sources.pop(id(source), None)

    def _prune_locked(self) -> None:
        now = self._clock()
        for source in tuple(self._sources.values()):
            if now >= source._expires:
                self._release_locked(source)

    def capture(
        self,
        value: object,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OwnedImageSource:
        # CRITICAL: a nonblocking gate prevents concurrent tensor copies from allocating before
        # accounting, or retaining an unbounded queue of caller-owned tensors while they wait.
        if not self._copy_gate.acquire(blocking=False):
            raise AuthoringSourceBindingError("image_copy_busy")
        reserved = 0
        try:
            try:
                torch = import_module("torch")
                numpy = import_module("numpy")
            except ImportError:
                raise AuthoringSourceBindingError("image_runtime_unavailable") from None
            if type(value) is not torch.Tensor:
                raise AuthoringSourceBindingError("source_unsupported")
            # Freeze the shape/stride header before reserving bytes. Keeping the caller's Tensor
            # header here lets a concurrent resize expand the copy after its smaller reservation.
            tensor_type = cast(Any, torch.Tensor)
            tensor = tensor_type.detach(value)
            if (
                tensor.device.type != "cpu"
                or tensor.dtype != torch.float32
                or tensor.layout != torch.strided
                or tensor.ndim != 4
                or tensor.shape[0] != 1
                or tensor.shape[3] != 3
                or tensor.is_conj()
                or tensor.is_neg()
            ):
                raise AuthoringSourceBindingError("source_unsupported")
            height, width = int(tensor.shape[1]), int(tensor.shape[2])
            size = width * height * 3 * 4
            if (
                not 1 <= width <= MAX_IMAGE_EDGE
                or not 1 <= height <= MAX_IMAGE_EDGE
                or width * height > MAX_IMAGE_PIXELS
                or size > MAX_IMAGE_SOURCE_BYTES
            ):
                raise AuthoringSourceBindingError("source_unsupported")
            with self._lock:
                self._prune_locked()
                if self._closed:
                    raise AuthoringSourceBindingError("store_closed")
                if len(self._sources) >= MAX_IMAGE_SOURCES:
                    raise AuthoringSourceBindingError("image_capacity")
                # Two pixel buffers plus a conservative full-buffer scratch reservation precede
                # every allocation. Source dtype is already exact; this never casts user input.
                reserved = 3 * size
                if self._live_bytes + self._reserved_bytes + reserved > self._max_bytes:
                    reserved = 0
                    raise AuthoringSourceBindingError("image_capacity")
                self._reserved_bytes += reserved
                deadline = self._clock() + self._ttl_seconds
            self._check_deadline(deadline, cancelled)
            owned = numpy.array(tensor_type.numpy(tensor), dtype="<f4", order="C", copy=True)
            if owned.shape != (1, height, width, 3) or owned.nbytes != size:
                raise AuthoringSourceBindingError("source_unsupported")
            # Validate the owned snapshot, not the caller's mutable tensor before copying it.
            if not numpy.isfinite(owned).all() or owned.min() < 0 or owned.max() > 1:
                raise AuthoringSourceBindingError("source_unsupported")
            self._check_deadline(deadline, cancelled)
            pixels = owned.tobytes(order="C")
            del owned
            source = OwnedImageSource(self, pixels, width, height, deadline)
            with self._lock:
                self._check_deadline(deadline, cancelled)
                if self._closed:
                    raise AuthoringSourceBindingError("store_closed")
                self._sources[id(source)] = source
                self._live_bytes += size
            return source
        except (MemoryError, OverflowError):
            raise AuthoringSourceBindingError("image_capacity") from None
        except RuntimeError as exc:
            if isinstance(exc, AuthoringSourceBindingError):
                raise
            raise AuthoringSourceBindingError("source_unsupported") from None
        finally:
            with self._lock:
                self._reserved_bytes -= reserved
            self._copy_gate.release()

    def _check_deadline(self, deadline: float, cancelled: Callable[[], bool] | None) -> None:
        now = self._clock()
        if not math.isfinite(now) or now >= deadline:
            raise AuthoringSourceBindingError("source_stale")
        if cancelled is not None and cancelled():
            raise AuthoringSourceBindingError("source_cancelled")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for source in tuple(self._sources.values()):
                self._release_locked(source)
