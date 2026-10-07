"""Closed retained-media integrity metadata; IDs and digests confer no use authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from .av_reconstruction import qualified_av_limits
from .composition_contract import MAX_LANDMARKS_PER_ASSET
from .durable_workspace_state import (
    MAX_OWNER_REVISION,
    DurableStateError,
    json_bytes,
    require_owner,
    strict_json,
)

RETAINED_CATALOG_SCHEMA = "h3.context.retained_asset_catalog.v1"
RETAINED_MEDIA_PROFILE = "h3.authoring.video_source_facts.v2"
MAX_CATALOG_BYTES = 32 * 1024
MAX_RETAINED_ASSET_BYTES = 64 * 1024 * 1024
MAX_OWNER_ASSETS = 16
MAX_GLOBAL_ASSETS = 64
MAX_PROJECT_REFERENCES = 8
CLOSED_RETENTION_MS = 7 * 86400 * 1000
_ASSET = re.compile(r"asset_[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"project_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASSET_FIELDS = frozenset(
    {
        "asset_id",
        "content_fingerprint",
        "byte_length",
        "media_profile",
        "width",
        "height",
        "frame_count",
        "duration_ms",
        "facts_fingerprint",
        "created_at_ms",
        "closed_at_ms",
        "project_references",
    }
)
_CATALOG_FIELDS = frozenset({"schema", "owner_id", "revision", "enabled", "assets"})


class RetainedAssetError(ValueError):
    """A bounded content-free refusal, never an exception containing a private locator."""

    CODES = DurableStateError.CODES | frozenset(
        {
            "asset_invalid",
            "catalog_invalid",
            "asset_unavailable",
            "asset_changed",
            "media_unqualified",
            "source_stale",
            "source_unavailable",
            "lease_invalid",
            "lease_bound",
            "asset_protected",
            "reference_invalid",
            "cancelled",
            "timed_out",
            "catalog_corrupt",
            "quota_assets",
        }
    )

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in self.CODES:
            raise ValueError("unknown retained asset refusal")
        self.code = code
        super().__init__(code)


def _identifier(value: object, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise RetainedAssetError(code)
    return value


def require_asset_id(value: object) -> str:
    return _identifier(value, _ASSET, "asset_invalid")


def require_project_reference(value: object) -> str:
    return _identifier(value, _PROJECT, "reference_invalid")


def _owner(value: object) -> str:
    try:
        return require_owner(value)
    except DurableStateError as error:
        raise RetainedAssetError(str(error)) from None


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RetainedAssetError("integer_invalid")
    return value


def _fields(value: object, expected: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise RetainedAssetError("shape_invalid")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class RetainedAsset:
    asset_id: str
    content_fingerprint: str
    byte_length: int
    media_profile: str
    width: int
    height: int
    frame_count: int
    duration_ms: int
    facts_fingerprint: str
    created_at_ms: int
    closed_at_ms: int | None
    project_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        limits = qualified_av_limits()
        require_asset_id(self.asset_id)
        _identifier(self.content_fingerprint, _SHA256, "asset_invalid")
        _identifier(self.facts_fingerprint, _SHA256, "asset_invalid")
        if type(self.media_profile) is not str or self.media_profile != RETAINED_MEDIA_PROFILE:
            raise RetainedAssetError("version_unsupported")
        _integer(self.byte_length, 1, MAX_RETAINED_ASSET_BYTES)
        _integer(self.width, 1, limits.max_width)
        _integer(self.height, 1, limits.max_height)
        _integer(self.frame_count, 1, MAX_LANDMARKS_PER_ASSET)
        _integer(self.duration_ms, 1, limits.max_duration_ms_per_segment)
        _integer(self.created_at_ms, 1, 9_999_999_999_999)
        if (
            type(self.project_references) is not tuple
            or len(self.project_references) > MAX_PROJECT_REFERENCES
            or any(type(value) is not str for value in self.project_references)
            or len(set(self.project_references)) != len(self.project_references)
        ):
            raise RetainedAssetError("reference_invalid")
        for value in self.project_references:
            require_project_reference(value)
        if self.project_references:
            if self.closed_at_ms is not None:
                raise RetainedAssetError("timestamp_invalid")
        else:
            _integer(self.closed_at_ms, self.created_at_ms, 9_999_999_999_999)

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "content_fingerprint": self.content_fingerprint,
            "byte_length": self.byte_length,
            "media_profile": self.media_profile,
            "width": self.width,
            "height": self.height,
            "frame_count": self.frame_count,
            "duration_ms": self.duration_ms,
            "facts_fingerprint": self.facts_fingerprint,
            "created_at_ms": self.created_at_ms,
            "closed_at_ms": self.closed_at_ms,
            "project_references": list(self.project_references),
        }


def decode_asset(value: object) -> RetainedAsset:
    row = _fields(value, _ASSET_FIELDS)
    references = row["project_references"]
    if type(references) is not list or len(references) > MAX_PROJECT_REFERENCES:
        raise RetainedAssetError("reference_invalid")
    closed = row["closed_at_ms"]
    profile = row["media_profile"]
    if type(profile) is not str or profile != RETAINED_MEDIA_PROFILE:
        raise RetainedAssetError("version_unsupported")
    return RetainedAsset(
        require_asset_id(row["asset_id"]),
        _identifier(row["content_fingerprint"], _SHA256, "asset_invalid"),
        _integer(row["byte_length"], 1, MAX_RETAINED_ASSET_BYTES),
        profile,
        _integer(row["width"], 1, qualified_av_limits().max_width),
        _integer(row["height"], 1, qualified_av_limits().max_height),
        _integer(row["frame_count"], 1, MAX_LANDMARKS_PER_ASSET),
        _integer(row["duration_ms"], 1, qualified_av_limits().max_duration_ms_per_segment),
        _identifier(row["facts_fingerprint"], _SHA256, "asset_invalid"),
        _integer(row["created_at_ms"], 1, 9_999_999_999_999),
        None if closed is None else _integer(closed, 1, 9_999_999_999_999),
        tuple(require_project_reference(value) for value in references),
    )


@dataclass(frozen=True, slots=True)
class RetainedCatalog:
    owner_id: str
    revision: int = 0
    enabled: bool = False
    assets: tuple[RetainedAsset, ...] = ()

    def __post_init__(self) -> None:
        _owner(self.owner_id)
        _integer(self.revision, 0, MAX_OWNER_REVISION)
        if type(self.enabled) is not bool:
            raise RetainedAssetError("catalog_invalid")
        if (
            type(self.assets) is not tuple
            or len(self.assets) > MAX_OWNER_ASSETS
            or any(type(value) is not RetainedAsset for value in self.assets)
            or len({value.asset_id for value in self.assets}) != len(self.assets)
            or (self.revision == 0 and (self.enabled or self.assets))
        ):
            raise RetainedAssetError("catalog_invalid")

    def to_wire(self) -> dict[str, object]:
        # CRITICAL: this catalog holds integrity facts only. Decoding it must never mint a
        # source binding or resurrect a saved generation receipt, even when its digest matches.
        return {
            "schema": RETAINED_CATALOG_SCHEMA,
            "owner_id": self.owner_id,
            "revision": self.revision,
            "enabled": self.enabled,
            "assets": [value.to_wire() for value in self.assets],
        }


def decode_catalog(payload: bytes, *, expected_owner: str) -> RetainedCatalog:
    owner = _owner(expected_owner)
    try:
        value = strict_json(payload, maximum_bytes=MAX_CATALOG_BYTES, maximum_depth=4)
    except DurableStateError as error:
        raise RetainedAssetError(str(error)) from None
    row = _fields(value, _CATALOG_FIELDS)
    if row["schema"] != RETAINED_CATALOG_SCHEMA:
        raise RetainedAssetError("version_unsupported")
    if _owner(row["owner_id"]) != owner:
        raise RetainedAssetError("owner_mismatch")
    raw = row["assets"]
    if type(raw) is not list or len(raw) > MAX_OWNER_ASSETS:
        raise RetainedAssetError("catalog_invalid")
    enabled = row["enabled"]
    if type(enabled) is not bool:
        raise RetainedAssetError("catalog_invalid")
    return RetainedCatalog(
        owner,
        _integer(row["revision"], 0, MAX_OWNER_REVISION),
        enabled,
        tuple(decode_asset(value) for value in raw),
    )


def encode_catalog(catalog: RetainedCatalog) -> bytes:
    if type(catalog) is not RetainedCatalog:
        raise RetainedAssetError("catalog_invalid")
    payload = json_bytes(catalog.to_wire())
    decode_catalog(payload, expected_owner=catalog.owner_id)
    return payload
