"""Lazy server-owned qualification for private single-user loopback recovery."""

from __future__ import annotations

import hashlib
import ipaddress
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.durable_workspace_state import DurableStateError
from ..core.request_target import PUBLIC_ORIGINS_VARIABLE
from .comfyui_route_seam import host_recovery_facts
from .media_runtime_resolution import (
    ComfyHostRootPort,
    HostRootError,
    HostRootPort,
    _link_free_existing_prefix,
    _overlaps,
)
from .segment_artifact_store import _validated_directories, _windows_dll


def _loopback(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def qualify_host(args: object, user_manager: object, *, public_origins_configured: bool) -> None:
    listen = getattr(args, "listen", None)
    users = getattr(user_manager, "users", None)
    if (
        getattr(args, "multi_user", None) is not False
        or getattr(args, "enable_cors_header", "missing") is not None
        or type(users) is not dict
        or users != {"default": "default"}
        or public_origins_configured is not False
        or type(listen) is not str
        or len(listen) > 512
    ):
        raise DurableStateError("host_unqualified")
    addresses = listen.split(",")
    # CRITICAL: server.address describes only the first listener. Every declared address must
    # be loopback; a trusted default profile or comfy-user header is not authentication.
    if not 1 <= len(addresses) <= 8 or not all(_loopback(value.strip()) for value in addresses):
        raise DurableStateError("host_unqualified")


def request_is_loopback(request: object) -> bool:
    reader = getattr(getattr(request, "transport", None), "get_extra_info", None)
    if not callable(reader):
        return False
    try:
        local, peer = reader("sockname"), reader("peername")
    except Exception:
        return False
    return all(
        type(value) is tuple and len(value) in (2, 4) and _loopback(value[0])
        for value in (local, peer)
    )


def fixed_ntfs(path: Path) -> bool:
    if os.name != "nt" or not path.is_absolute() or not _link_free_existing_prefix(path):
        return False
    import ctypes
    from ctypes import wintypes

    nearest = path
    while not nearest.exists():
        if nearest == nearest.parent:
            return False
        nearest = nearest.parent
    try:
        # The native volume query follows junctions, so it must never precede prefix validation.
        with _validated_directories(nearest):
            kernel = _windows_dll("kernel32")
            root = ctypes.create_unicode_buffer(32768)
            volume_path = kernel.GetVolumePathNameW
            volume_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
            volume_path.restype = wintypes.BOOL
            if not volume_path(str(nearest), root, len(root)):
                return False
            drive_type = kernel.GetDriveTypeW
            drive_type.argtypes, drive_type.restype = (wintypes.LPCWSTR,), wintypes.UINT
            if drive_type(root.value) != 3:
                return False
            filesystem = ctypes.create_unicode_buffer(64)
            volume = kernel.GetVolumeInformationW
            volume.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPWSTR,
                wintypes.DWORD,
            )
            volume.restype = wintypes.BOOL
            return (
                bool(volume(root.value, None, 0, None, None, None, filesystem, len(filesystem)))
                and filesystem.value.upper() == "NTFS"
            )
    except (OSError, ValueError, RuntimeError):
        return False


@dataclass(frozen=True, slots=True)
class RecoveryOwner:
    owner_id: str
    private_root: Path = field(repr=False)


class RecoveryOwnerPort:
    def __init__(
        self,
        *,
        host: HostRootPort | None = None,
        host_facts: Callable[[], tuple[object, object]] = host_recovery_facts,
        public_origins: Callable[[], bool] = lambda: PUBLIC_ORIGINS_VARIABLE in os.environ,
        filesystem: Callable[[Path], bool] = fixed_ntfs,
    ) -> None:
        self._host = host or ComfyHostRootPort()
        self._facts, self._public, self._filesystem = host_facts, public_origins, filesystem

    def resolve(self) -> RecoveryOwner:
        qualify_host(*self._facts(), public_origins_configured=self._public())
        try:
            private, served = self._host.private_root(), self._host.served_roots()
            if (
                not isinstance(private, Path)
                or not private.is_absolute()
                or len(served) != 3
                or any(not isinstance(root, Path) or not root.is_absolute() for root in served)
                or not _link_free_existing_prefix(private)
                or any(_overlaps(private, root) for root in served)
            ):
                raise DurableStateError("storage_unsafe")
            if self._filesystem(private) is not True:
                raise DurableStateError("filesystem_unqualified")
        except HostRootError as error:
            raise DurableStateError("host_unqualified") from error
        namespace = "h3.workspace-state.owner.v1\0" + os.path.normcase(str(private)) + "\0default"
        identifier = "owner_" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32]
        return RecoveryOwner(identifier, private)
