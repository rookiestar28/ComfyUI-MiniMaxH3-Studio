"""Exact Windows executable admission shared by media use and media runtime discovery.

The primitive opens one existing executable without write/delete sharing, hashes the pinned bytes
and re-checks the file identity. It never executes anything. `QualifiedAVMediaAdapter` wraps it
for every spawn, and the media runtime discovery worker uses it to admit a candidate pair, so the
two cannot drift into different notions of which file is the qualified tool.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.safe_paths import UnsafePathError, validate_regular_file
from .segment_artifact_store import (
    ArtifactStoreError,
    _identity,
    _is_link_or_reparse,
    _validated_directories,
)

MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

FileIdentity = tuple[int, int, int, int]


class ExecutableAdmissionError(RuntimeError):
    """Content-free admission failure with one closed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def windows_open_read_pin(path: Path) -> int:
    """Open an existing regular file without write/delete sharing."""

    if os.name != "nt":
        raise OSError("Windows pin unavailable")
    import ctypes
    import msvcrt

    # CRITICAL: Linux Python stubs omit Windows-only ctypes/msvcrt symbols; this branch is
    # reached only for the explicitly gated Windows pin path.
    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
    get_last_error = getattr(ctypes, "get_last_error")  # noqa: B009
    open_osfhandle = getattr(msvcrt, "open_osfhandle")  # noqa: B009
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    # SECURITY: FILE_SHARE_WRITE/DELETE would permit same-identity content replacement after hash.
    handle = create_file(
        str(path),
        0x80000000,
        0x00000001,
        None,
        3,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise OSError(get_last_error(), "CreateFileW read pin failed")
    try:
        return int(
            open_osfhandle(
                int(handle),
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        )
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


_SCOPE = threading.local()


@contextmanager
def executable_pin_scope() -> Iterator[None]:
    """Verify each executable once for the media operation the calling thread runs in this block.

    Inside a scope the first pin of a file hashes it and keeps that descriptor; later pins of the
    same file and digest re-check the held descriptor's identity instead of hashing again. The
    descriptor is the proof: it was opened without write or delete sharing, so the hashed bytes
    cannot change while it is held. Every descriptor is closed when the outermost scope exits.

    CRITICAL: a scope must not outlive one media operation, and must sit inside the media runtime
    lease. A descriptor held between operations blocks the replacement of the runtime's own files,
    which the runtime manager performs before it detaches the old binding.
    """

    if getattr(_SCOPE, "pins", None) is not None:
        yield
        return
    pins: dict[tuple[str, str], tuple[int, FileIdentity]] = {}
    _SCOPE.pins = pins
    try:
        yield
    finally:
        _SCOPE.pins = None
        for descriptor, _ in pins.values():
            try:
                os.close(descriptor)
            except OSError:
                pass


@contextmanager
def pin_exact_executable(
    path: Path,
    expected_sha256: str,
    *,
    charge: Callable[[int], None] | None = None,
    passthrough: tuple[type[BaseException], ...] = (),
) -> Iterator[FileIdentity]:
    """Pin and hash one exact Windows executable across process spawn/use.

    `charge` receives the size of every chunk before it is hashed and may raise an
    `ExecutableAdmissionError` to stop a caller whose total byte or time budget is exhausted.
    `passthrough` names exception types that propagate unchanged from the pinned block, which is
    how the adapter keeps its own closed errors from being re-coded as a capability change.
    """

    # CRITICAL: the current POSIX runner spawns by pathname, so a descriptor-only
    # validation cannot guarantee the child executes the hashed file; fail closed until
    # a true fd-based POSIX spawn path is implemented.
    if os.name != "nt":
        raise ExecutableAdmissionError("adapter_unavailable")
    pins: dict[tuple[str, str], tuple[int, FileIdentity]] | None = getattr(_SCOPE, "pins", None)
    key = (os.path.normcase(str(path)), expected_sha256.casefold())
    held = None if pins is None else pins.get(key)
    descriptor: int | None = None
    owned = True
    try:
        with _validated_directories(path.parent):
            admitted = validate_regular_file(path, maximum_bytes=MAX_EXECUTABLE_BYTES)
            before = admitted.lstat()
            if before.st_nlink != 1 or _is_link_or_reparse(admitted, before):
                raise ExecutableAdmissionError("adapter_capability_changed")
            if held is not None:
                # Verified earlier in this scope. The held descriptor still denies writers, so
                # only the identity of the path and of the descriptor is checked again.
                descriptor, identity = held
                owned = False
                if _identity(before) != identity or _identity(os.fstat(descriptor)) != identity:
                    raise ExecutableAdmissionError("adapter_capability_changed")
            else:
                descriptor = windows_open_read_pin(admitted)
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or _identity(opened) != _identity(before)
                ):
                    raise ExecutableAdmissionError("adapter_capability_changed")
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_EXECUTABLE_BYTES:
                        raise ExecutableAdmissionError("adapter_capability_changed")
                    if charge is not None:
                        charge(len(chunk))
                    digest.update(chunk)
                after = admitted.lstat()
                if (
                    after.st_nlink != 1
                    or _is_link_or_reparse(admitted, after)
                    or _identity(after) != _identity(opened)
                    or digest.hexdigest().casefold() != expected_sha256.casefold()
                ):
                    raise ExecutableAdmissionError("adapter_capability_changed")
                identity = _identity(opened)
                if pins is not None:
                    pins[key] = (descriptor, identity)
                    owned = False
            yield identity
            final = admitted.lstat()
            if (
                final.st_nlink != 1
                or _is_link_or_reparse(admitted, final)
                or _identity(final) != identity
                or _identity(os.fstat(descriptor)) != identity
            ):
                raise ExecutableAdmissionError("adapter_capability_changed")
    except ExecutableAdmissionError:
        raise
    except BaseException as exc:
        # IMPORTANT: a caller's own closed error must leave the pin exactly as it entered. The
        # adapter's error type is itself a RuntimeError, so without this check the generic
        # translation below would turn every in-use media failure into a capability change.
        if passthrough and isinstance(exc, passthrough):
            raise
        if isinstance(exc, (ArtifactStoreError, UnsafePathError, OSError, RuntimeError)):
            raise ExecutableAdmissionError("adapter_capability_changed") from exc
        raise
    finally:
        if descriptor is not None and owned:
            os.close(descriptor)


__all__ = [
    "MAX_EXECUTABLE_BYTES",
    "ExecutableAdmissionError",
    "FileIdentity",
    "executable_pin_scope",
    "pin_exact_executable",
    "windows_open_read_pin",
]
