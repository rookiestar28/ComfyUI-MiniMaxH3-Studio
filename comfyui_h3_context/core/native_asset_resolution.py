"""Current, content-free advisory observation of official materialization roles."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import NativeH3AdapterError
from .generation_profile import HostObservation, qualify_generation_profile

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
MATERIALIZATION_ASSET_ROLES = (
    "video_unet",
    "reference_unet",
    "text_encoder",
    "video_vae",
    "audio_vae",
    "image_turbo_lora",
    "reference_turbo_lora",
)


@dataclass(frozen=True, slots=True)
class NativeAssetResolutionV1:
    """Pin bounded inventory observations without selecting or exposing a model filename."""

    host_profile_fingerprint: str
    inventory_fingerprint: str
    resolved_roles: tuple[str, ...]
    _observe: Callable[[], tuple[str, str, tuple[str, ...]]] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.host_profile_fingerprint) is not str
            or _HASH.fullmatch(self.host_profile_fingerprint) is None
            or type(self.inventory_fingerprint) is not str
            or _HASH.fullmatch(self.inventory_fingerprint) is None
            or type(self.resolved_roles) is not tuple
            # CRITICAL: an empty/partial official-name match is valid advice. Claim only the
            # known matching roles; filename differences must not refuse managed readiness.
            or self.resolved_roles
            != tuple(role for role in MATERIALIZATION_ASSET_ROLES if role in self.resolved_roles)
            or not callable(self._observe)
        ):
            raise NativeH3AdapterError("native_asset_resolution_unqualified", "resolution invalid")

    def assert_current(self, host: HostObservation | None = None) -> None:
        try:
            if type(self) is not NativeAssetResolutionV1:
                raise ValueError("resolution type")
            if (
                host is not None
                and qualify_generation_profile(host).fingerprint() != self.host_profile_fingerprint
            ):
                raise ValueError("host changed")
            if self._observe() != (
                self.host_profile_fingerprint,
                self.inventory_fingerprint,
                self.resolved_roles,
            ):
                raise ValueError("inventory changed")
        except Exception:
            raise NativeH3AdapterError(
                "native_asset_resolution_stale", "resolution facts changed"
            ) from None
