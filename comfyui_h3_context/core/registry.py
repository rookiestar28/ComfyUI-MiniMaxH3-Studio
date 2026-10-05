"""Deterministic canonical asset ownership and native H3 reference labels.

This module records metadata supplied by a caller; it never opens or probes a media source. The
native ComfyUI label vocabulary is derived exactly once from explicit presentation order and kept
separate from canonical asset values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .contracts import AssetDescriptor, AssetRole, MediaKind
from .errors import (
    ContractValidationError,
    ReferenceLabelError,
    ReferenceOrderError,
    ReferenceRegistryError,
)

MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_PAIRED_VIDEO_AUDIO = 3
MAX_STANDALONE_AUDIO = 3
MAX_CONNECTION_ORDER = 1000
MAX_WIDTH = 1_048_576
MAX_HEIGHT = 1_048_576
# M17-25: a media-side sanity bound on a declared source frame count, not the
# H3 output range. It carried the H3 name and invited exactly the confusion the
# single alignment authority exists to prevent.
MAX_MEDIA_FRAME_COUNT = 10_000_000
MAX_SAMPLE_RATE = 768_000
MAX_CHANNELS = 256
MAX_DURATION_SECONDS = Decimal("86400")


class BackendTarget(str, Enum):
    """Supported label-emission boundary."""

    COMFYUI_H3 = "comfyui_h3"


class BackendLabelKind(str, Enum):
    """Independent native H3 reference label families."""

    PICTURE = "picture"
    VIDEO = "video"
    AUDIO = "audio"


def _require_identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "_.:-"))
            for character in value
        )
        or not value[0].isascii()
        or not value[0].isalnum()
    ):
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_positive_int(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ReferenceRegistryError(f"{field} must be an integer between 1 and {maximum}")
    return value


def _require_optional_positive_int(value: object, field: str, maximum: int) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field, maximum)


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Optional caller-supplied media facts; absence never triggers media probing."""

    duration_seconds: Decimal | None = None
    width: int | None = None
    height: int | None = None
    frame_count: int | None = None
    sample_rate: int | None = None
    channels: int | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds is not None and (
            not isinstance(self.duration_seconds, Decimal)
            or not self.duration_seconds.is_finite()
            or not 0 < self.duration_seconds <= MAX_DURATION_SECONDS
        ):
            raise ReferenceRegistryError(
                "duration_seconds must be a finite Decimal between 0 and 86400"
            )
        _require_optional_positive_int(self.width, "width", MAX_WIDTH)
        _require_optional_positive_int(self.height, "height", MAX_HEIGHT)
        _require_optional_positive_int(self.frame_count, "frame_count", MAX_MEDIA_FRAME_COUNT)
        _require_optional_positive_int(self.sample_rate, "sample_rate", MAX_SAMPLE_RATE)
        _require_optional_positive_int(self.channels, "channels", MAX_CHANNELS)

    def to_wire(self) -> dict[str, int | str | None]:
        return {
            "duration_seconds": (
                None if self.duration_seconds is None else format(self.duration_seconds, "f")
            ),
            "width": self.width,
            "height": self.height,
            "frame_count": self.frame_count,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


@dataclass(frozen=True, slots=True)
class ReferenceAsset:
    """Stable canonical asset identity independent from backend label text."""

    asset_id: str
    kind: MediaKind
    role: AssetRole
    connection_order: int
    metadata: MediaMetadata | None = None
    paired_video_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.asset_id, "asset_id")
        if not isinstance(self.kind, MediaKind):
            raise ReferenceRegistryError("reference asset kind must be a MediaKind")
        if not isinstance(self.role, AssetRole):
            raise ReferenceRegistryError("reference asset role must be an AssetRole")
        _require_positive_int(self.connection_order, "connection_order", MAX_CONNECTION_ORDER)
        if self.metadata is not None and not isinstance(self.metadata, MediaMetadata):
            raise ReferenceRegistryError("metadata must be a MediaMetadata or None")
        if self.paired_video_id is not None:
            if self.kind is not MediaKind.AUDIO:
                raise ReferenceRegistryError("only audio assets may pair with a video")
            _require_identifier(self.paired_video_id, "paired_video_id")
        if self.kind is MediaKind.IMAGE and self.role in {
            AssetRole.FIRST_FRAME,
            AssetRole.LAST_FRAME,
        }:
            return
        if self.role in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}:
            raise ReferenceRegistryError("frame-anchor roles require image assets")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "connection_order": self.connection_order,
            "metadata": None if self.metadata is None else self.metadata.to_wire(),
            "paired_video_id": self.paired_video_id,
        }

    def to_descriptor(self) -> AssetDescriptor:
        return AssetDescriptor(self.asset_id, self.kind, self.role)


@dataclass(frozen=True, slots=True)
class BackendLabel:
    """A backend-specific label derived from a canonical asset, never stored on it."""

    backend: BackendTarget
    kind: BackendLabelKind
    ordinal: int
    asset_id: str
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.backend, BackendTarget):
            raise ReferenceLabelError("backend must be a BackendTarget")
        if not isinstance(self.kind, BackendLabelKind):
            raise ReferenceLabelError("label kind must be a BackendLabelKind")
        _require_positive_int(self.ordinal, "label ordinal", MAX_CONNECTION_ORDER)
        _require_identifier(self.asset_id, "label asset_id")
        expected = _label_text(self.kind, self.ordinal)
        if self.label != expected:
            raise ReferenceLabelError(
                f"label for {self.asset_id} must be {expected!r}, not {self.label!r}"
            )

    def to_wire(self) -> dict[str, str | int]:
        return {
            "backend": self.backend.value,
            "kind": self.kind.value,
            "ordinal": self.ordinal,
            "asset_id": self.asset_id,
            "label": self.label,
        }


def _label_text(kind: BackendLabelKind, ordinal: int) -> str:
    prefix = {
        BackendLabelKind.PICTURE: "Picture",
        BackendLabelKind.VIDEO: "Video",
        BackendLabelKind.AUDIO: "Audio",
    }[kind]
    return f"<{prefix} {ordinal}>"


def _validate_asset_sequence(assets: tuple[ReferenceAsset, ...]) -> None:
    if not assets:
        return
    IDs = [asset.asset_id for asset in assets]
    if len(IDs) != len(set(IDs)):
        raise ReferenceRegistryError("reference asset IDs must be unique")
    orders = [asset.connection_order for asset in assets]
    expected_orders = list(range(1, len(assets) + 1))
    if orders != expected_orders:
        raise ReferenceOrderError(
            "reference assets must be in 1-based contiguous connection order without reordering"
        )

    by_id = {asset.asset_id: asset for asset in assets}
    paired_by_video: dict[str, str] = {}
    image_count = 0
    video_count = 0
    paired_count = 0
    standalone_count = 0
    phase = "images"
    for index, asset in enumerate(assets):
        if asset.kind is MediaKind.IMAGE:
            image_count += 1
            if phase != "images":
                raise ReferenceOrderError("all reference images must precede videos and audio")
            continue

        if asset.kind is MediaKind.VIDEO:
            video_count += 1
            if phase == "audio":
                raise ReferenceOrderError("videos cannot follow standalone reference audio")
            phase = "videos"
            continue

        if asset.paired_video_id is not None:
            paired_count += 1
            if phase == "audio":
                raise ReferenceOrderError("paired video audio cannot follow standalone audio")
            target = by_id.get(asset.paired_video_id)
            if target is None or target.kind is not MediaKind.VIDEO:
                raise ReferenceRegistryError(
                    f"paired video {asset.paired_video_id!r} is missing or not a video"
                )
            if asset.paired_video_id in paired_by_video:
                raise ReferenceRegistryError(
                    f"video {asset.paired_video_id!r} has more than one paired soundtrack"
                )
            if index + 1 >= len(assets) or assets[index + 1].asset_id != asset.paired_video_id:
                raise ReferenceOrderError(
                    "a paired soundtrack must appear immediately before its video"
                )
            paired_by_video[asset.paired_video_id] = asset.asset_id
            phase = "videos"
            continue

        standalone_count += 1
        phase = "audio"

    if image_count > MAX_REFERENCE_IMAGES:
        raise ReferenceRegistryError(
            f"at most {MAX_REFERENCE_IMAGES} reference images are supported"
        )
    if video_count > MAX_REFERENCE_VIDEOS:
        raise ReferenceRegistryError(
            f"at most {MAX_REFERENCE_VIDEOS} reference videos are supported"
        )
    if paired_count > MAX_PAIRED_VIDEO_AUDIO:
        raise ReferenceRegistryError(
            f"at most {MAX_PAIRED_VIDEO_AUDIO} paired video soundtracks are supported"
        )
    if standalone_count > MAX_STANDALONE_AUDIO:
        raise ReferenceRegistryError(
            f"at most {MAX_STANDALONE_AUDIO} standalone reference audio assets are supported"
        )


def _derive_labels(assets: tuple[ReferenceAsset, ...]) -> tuple[BackendLabel, ...]:
    counters = {
        MediaKind.IMAGE: 0,
        MediaKind.VIDEO: 0,
        MediaKind.AUDIO: 0,
    }
    kinds = {
        MediaKind.IMAGE: BackendLabelKind.PICTURE,
        MediaKind.VIDEO: BackendLabelKind.VIDEO,
        MediaKind.AUDIO: BackendLabelKind.AUDIO,
    }
    labels: list[BackendLabel] = []
    for asset in assets:
        counters[asset.kind] += 1
        kind = kinds[asset.kind]
        ordinal = counters[asset.kind]
        labels.append(
            BackendLabel(
                backend=BackendTarget.COMFYUI_H3,
                kind=kind,
                ordinal=ordinal,
                asset_id=asset.asset_id,
                label=_label_text(kind, ordinal),
            )
        )
    return tuple(labels)


@dataclass(frozen=True, slots=True)
class ReferenceRegistry:
    """Validated canonical assets and the separate derived native-label vector."""

    assets: tuple[ReferenceAsset, ...] = ()
    labels: tuple[BackendLabel, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.assets, tuple) or not all(
            isinstance(asset, ReferenceAsset) for asset in self.assets
        ):
            raise ReferenceRegistryError("assets must be a tuple of ReferenceAsset values")
        if not isinstance(self.labels, tuple) or not all(
            isinstance(label, BackendLabel) for label in self.labels
        ):
            raise ReferenceRegistryError("labels must be a tuple of BackendLabel values")
        _validate_asset_sequence(self.assets)
        expected = _derive_labels(self.assets)
        if self.labels != expected:
            raise ReferenceLabelError("backend labels do not match the deterministic asset vector")

    @classmethod
    def empty(cls) -> ReferenceRegistry:
        return cls()

    def label_for(self, asset_id: str) -> BackendLabel:
        for label in self.labels:
            if label.asset_id == asset_id:
                return label
        raise ReferenceRegistryError(f"no backend label exists for asset {asset_id!r}")

    def to_asset_descriptors(self) -> tuple[AssetDescriptor, ...]:
        return tuple(asset.to_descriptor() for asset in self.assets)

    def to_wire(self) -> dict[str, object]:
        return {
            "assets": [asset.to_wire() for asset in self.assets],
            "labels": [label.to_wire() for label in self.labels],
        }


def build_reference_registry(
    values: Iterable[ReferenceAsset] | ReferenceRegistry,
) -> ReferenceRegistry:
    """Copy and validate caller assets, deriving native labels without media access."""

    if isinstance(values, ReferenceRegistry):
        return values
    try:
        assets = tuple(values)
    except TypeError as exc:
        raise ReferenceRegistryError("reference assets must be an iterable") from exc
    if not all(isinstance(asset, ReferenceAsset) for asset in assets):
        raise ReferenceRegistryError("reference assets must contain only ReferenceAsset values")
    _validate_asset_sequence(assets)
    return ReferenceRegistry(assets=assets, labels=_derive_labels(assets))


__all__ = [
    "BackendLabel",
    "BackendLabelKind",
    "BackendTarget",
    "MediaMetadata",
    "ReferenceAsset",
    "ReferenceRegistry",
    "ReferenceRegistryError",
    "MAX_PAIRED_VIDEO_AUDIO",
    "MAX_REFERENCE_IMAGES",
    "MAX_REFERENCE_VIDEOS",
    "MAX_STANDALONE_AUDIO",
    "build_reference_registry",
]
