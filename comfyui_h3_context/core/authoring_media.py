"""Closed, content-free requests and values for private Authoring derivatives."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from .composition_contract import (
    ENGINE_PROFILE_ID,
    MAX_LANDMARKS_PER_ASSET,
    DerivativeManifest,
    PrivateSourceManifest,
    PublicAsset,
    PublicCompositionSnapshot,
    composition_contract_fingerprint,
)

REQUEST_SCHEMA = "h3.context.authoring_media_lease.request.v1"
NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA = "h3.context.authoring_asset_lease.request.v1"
SUCCESS_SCHEMA = "h3.context.authoring_media_lease.success.v1"
RELEASED_SCHEMA = "h3.context.authoring_media_lease.released.v1"
ERROR_SCHEMA = "h3.context.authoring_media_lease.error.v1"
DERIVATIVE_PROFILE_ID = "h3.authoring.media_derivatives.v6"
RUNTIME_PROFILE_FINGERPRINT = (
    "sha256:f0a226902d89e48113905b43c09fd99c63b2307f8d39ae740330431e091861ff"
)
MAX_CONTROL_BYTES = 8192
# CRITICAL: leases read the complete admitted source. A smaller span rejects valid imported
# videos before proxy generation; independent derivative byte and lease quotas still apply.
MAX_SOURCE_FRAMES = MAX_LANDMARKS_PER_ASSET
MAX_INTEGER = 9_007_199_254_740_991
LEASE_TTL_SECONDS = 60
LEASE_LIFETIME_SECONDS = 900
MAX_LEASES = 16
MAX_WORKSPACE_LEASES = 8
MAX_WORKSPACE_VIDEO_LEASES = 3
MAX_WORKSPACE_DECORATION_LEASES = 2
MAX_CACHE_BYTES = 128 * 1024 * 1024
# How long the product keeps a derivative body resident after its last lease ends, so that the
# lease an accepted edit asks for next is given that body and nothing is generated again. A
# body generated again is another body: a proxy encoded on more than one thread can differ
# from run to run, so it has another fingerprint and a browser cannot take it for its own.
# The period bounds idleness only: the body shares MAX_CACHE_BYTES and is dropped earlier when
# its source ends.
RETAINED_DERIVATIVE_IDLE_SECONDS = 900
MAX_MEDIA_GEOMETRY_EDGE = 16_384
DerivativeKind = Literal[
    "video_proxy",
    "audio_preview",
    "frame_timing_index",
    "thumbnail",
    "filmstrip",
    "audio_peaks",
    "image_proxy",
    "packaged_font_face",
]
LeaseScope = Literal["clip", "asset"]
AudioDisposition = Literal["present_bound", "absent", "unavailable", "excluded_overlay_policy"]
Operation = Literal["open", "renew", "transfer", "release"]
KINDS: tuple[DerivativeKind, ...] = (
    "video_proxy",
    "audio_preview",
    "frame_timing_index",
    "thumbnail",
    "filmstrip",
    "audio_peaks",
    "image_proxy",
    "packaged_font_face",
)
AUDIO_DISPOSITIONS = ("present_bound", "absent", "unavailable", "excluded_overlay_policy")
# What a request may name without a clip. A decoration is drawn for an asset; a playback kind is
# prepared for one, so that the clip that uses the asset later finds the body already generated.
ASSET_DECORATION_KINDS: frozenset[DerivativeKind] = frozenset(
    {"thumbnail", "filmstrip", "audio_peaks"}
)
ASSET_PREPARATION_KINDS: frozenset[DerivativeKind] = frozenset({"video_proxy", "audio_preview"})
REASONS = frozenset(
    {
        "invalid_request",
        "authority_mismatch",
        "stale",
        "lease_gone",
        "unsupported",
        "resource_limit",
        "busy",
        "cancelled",
        "timeout",
        "generation_failed",
        "internal_failure",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BASE_KEYS = frozenset({"schema", "operation", "requestId"})
_CREATE_KEYS = _BASE_KEYS | {
    "workspaceHandle",
    "workspaceRevision",
    "timelineRevision",
    "publicFingerprint",
    "manifestFingerprint",
    "profileFingerprint",
    "scope",
    "clipId",
    "assetId",
    "derivativeKind",
    "ownerId",
    "runtimeEpoch",
    "sourceStartFrame",
    "sourceEndFrame",
}
_NLE_AUTHORING_ASSET_CREATE_KEYS = _BASE_KEYS | {
    "authoringSchema",
    "profileId",
    "workspaceHandle",
    "workspaceRevision",
    "timelineRevision",
    "authoringFingerprint",
    "manifestFingerprint",
    "profileFingerprint",
    "assetId",
    "derivativeKind",
    "ownerId",
    "runtimeEpoch",
    "sourceStartFrame",
    "sourceEndFrame",
}
_COMMAND_KEYS = _BASE_KEYS | {"leaseId", "revision", "ownerId", "runtimeEpoch"}


class MediaLeaseError(ValueError):
    """A closed disposition that never includes a private input or exception."""

    def __init__(self, reason: str = "invalid_request") -> None:
        self.reason = reason if reason in REASONS else "internal_failure"
        super().__init__(self.reason)


def require_identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise MediaLeaseError()
    return value


def require_fingerprint(value: object) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise MediaLeaseError()
    return value


def require_integer(value: object, minimum: int = 1, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise MediaLeaseError()
    return value


def derivative_media_type(kind: DerivativeKind) -> str:
    return {
        "video_proxy": "video/mp4",
        "audio_preview": "audio/wav",
        "frame_timing_index": "application/json",
        "thumbnail": "image/png",
        "filmstrip": "image/jpeg",
        "audio_peaks": "application/octet-stream",
        "image_proxy": "image/png",
        "packaged_font_face": "font/ttf",
    }[kind]


def derivative_byte_limit(kind: DerivativeKind) -> int:
    return {
        "video_proxy": 24 * 1024 * 1024,
        "audio_preview": 8 * 1024 * 1024,
        "frame_timing_index": 16 * 1024,
        "thumbnail": 512 * 1024,
        "filmstrip": 512 * 1024,
        "audio_peaks": 64 * 1024,
        "image_proxy": 16 * 1024 * 1024,
        "packaged_font_face": 1024 * 1024,
    }[kind]


@dataclass(frozen=True, slots=True)
class CreateLeaseRequest:
    request_id: str
    workspace_handle: str
    workspace_revision: int
    timeline_revision: int
    public_fingerprint: str | None
    manifest_fingerprint: str
    profile_fingerprint: str
    scope: LeaseScope
    clip_id: str | None
    asset_id: str
    derivative_kind: DerivativeKind
    owner_id: str
    runtime_epoch: int
    source_start_frame: int
    source_end_frame: int
    request_schema: str = REQUEST_SCHEMA
    authoring_schema: str | None = None
    authoring_profile_id: str | None = None
    authoring_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.request_id,
            self.workspace_handle,
            self.asset_id,
            self.owner_id,
        ):
            require_identifier(value)
        if self.scope == "clip":
            require_identifier(self.clip_id)
        elif self.scope == "asset":
            if self.clip_id is not None:
                raise MediaLeaseError()
        else:
            raise MediaLeaseError()
        for number in (self.workspace_revision, self.timeline_revision, self.runtime_epoch):
            require_integer(number)
        for fingerprint in (self.manifest_fingerprint, self.profile_fingerprint):
            require_fingerprint(fingerprint)
        if self.profile_fingerprint != RUNTIME_PROFILE_FINGERPRINT:
            raise MediaLeaseError("unsupported")
        if self.request_schema == REQUEST_SCHEMA:
            if (
                self.public_fingerprint is None
                or self.authoring_schema is not None
                or self.authoring_profile_id is not None
                or self.authoring_fingerprint is not None
            ):
                raise MediaLeaseError()
            require_fingerprint(self.public_fingerprint)
        elif self.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
            from .nle_authoring_contract import NLE_AUTHORING_PROFILE_ID, NLE_AUTHORING_SCHEMA

            if (
                self.scope != "asset"
                or self.clip_id is not None
                or self.public_fingerprint is not None
                or self.authoring_schema != NLE_AUTHORING_SCHEMA
                or self.authoring_profile_id != NLE_AUTHORING_PROFILE_ID
            ):
                raise MediaLeaseError("unsupported")
            require_fingerprint(self.authoring_fingerprint)
        else:
            raise MediaLeaseError("unsupported")
        if self.derivative_kind not in KINDS:
            raise MediaLeaseError("unsupported")
        # IMPORTANT: the playback kinds are admitted without a clip in the authoring-bound request
        # only, where a catalog asset is prepared before any clip uses it. The snapshot request's
        # asset scope stays the three decorations; widening it here would admit a playback body
        # for a form that no producer sends and that the browser's codec refuses.
        asset_kinds = (
            ASSET_PREPARATION_KINDS | ASSET_DECORATION_KINDS
            if self.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA
            else ASSET_DECORATION_KINDS
        )
        if self.scope == "asset" and self.derivative_kind not in asset_kinds:
            raise MediaLeaseError("unsupported")
        require_integer(self.source_start_frame, 0, MAX_SOURCE_FRAMES - 1)
        require_integer(self.source_end_frame, self.source_start_frame + 1, MAX_SOURCE_FRAMES)

    def to_wire(self) -> dict[str, object]:
        if self.request_schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
            return {
                "schema": self.request_schema,
                "operation": "create",
                "requestId": self.request_id,
                "authoringSchema": self.authoring_schema,
                "profileId": self.authoring_profile_id,
                "workspaceHandle": self.workspace_handle,
                "workspaceRevision": self.workspace_revision,
                "timelineRevision": self.timeline_revision,
                "authoringFingerprint": self.authoring_fingerprint,
                "manifestFingerprint": self.manifest_fingerprint,
                "profileFingerprint": self.profile_fingerprint,
                "assetId": self.asset_id,
                "derivativeKind": self.derivative_kind,
                "ownerId": self.owner_id,
                "runtimeEpoch": self.runtime_epoch,
                "sourceStartFrame": self.source_start_frame,
                "sourceEndFrame": self.source_end_frame,
            }
        return {
            "schema": self.request_schema,
            "operation": "create",
            "requestId": self.request_id,
            "workspaceHandle": self.workspace_handle,
            "workspaceRevision": self.workspace_revision,
            "timelineRevision": self.timeline_revision,
            "publicFingerprint": self.public_fingerprint,
            "manifestFingerprint": self.manifest_fingerprint,
            "profileFingerprint": self.profile_fingerprint,
            "scope": self.scope,
            "clipId": self.clip_id,
            "assetId": self.asset_id,
            "derivativeKind": self.derivative_kind,
            "ownerId": self.owner_id,
            "runtimeEpoch": self.runtime_epoch,
            "sourceStartFrame": self.source_start_frame,
            "sourceEndFrame": self.source_end_frame,
        }


@dataclass(frozen=True, slots=True)
class LeaseCommand:
    operation: Operation
    request_id: str
    lease_id: str
    revision: int
    owner_id: str
    runtime_epoch: int
    next_owner_id: str | None = None
    next_runtime_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.operation not in ("open", "renew", "transfer", "release"):
            raise MediaLeaseError()
        for value in (self.request_id, self.lease_id, self.owner_id):
            require_identifier(value)
        for number in (self.revision, self.runtime_epoch):
            require_integer(number)
        if self.operation == "transfer":
            require_identifier(self.next_owner_id)
            require_integer(self.next_runtime_epoch)
            if (self.next_owner_id, self.next_runtime_epoch) == (self.owner_id, self.runtime_epoch):
                raise MediaLeaseError()
        elif self.next_owner_id is not None or self.next_runtime_epoch is not None:
            raise MediaLeaseError()

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "schema": REQUEST_SCHEMA,
            "operation": self.operation,
            "requestId": self.request_id,
            "leaseId": self.lease_id,
            "revision": self.revision,
            "ownerId": self.owner_id,
            "runtimeEpoch": self.runtime_epoch,
        }
        if self.operation == "transfer":
            wire.update(nextOwnerId=self.next_owner_id, nextRuntimeEpoch=self.next_runtime_epoch)
        return wire


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MediaLeaseError()
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise MediaLeaseError()


def decode_lease_request(body: bytes) -> CreateLeaseRequest | LeaseCommand:
    if type(body) is not bytes or not 1 <= len(body) <= MAX_CONTROL_BYTES:
        raise MediaLeaseError()
    try:
        wire = json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant
        )
        if type(wire) is not dict:
            raise MediaLeaseError()
        schema = wire.get("schema")
        operation = wire.get("operation")
        if schema not in {REQUEST_SCHEMA, NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA}:
            raise MediaLeaseError()
        # CRITICAL: the protocol is flat and closed. Reject nested/private input before any
        # authority lookup; permissive JSON forwarding turns an opaque lease into a path API.
        if any(
            type(value) not in (str, int)
            and not (
                operation == "create"
                and schema == REQUEST_SCHEMA
                and key == "clipId"
                and value is None
            )
            for key, value in wire.items()
        ):
            raise MediaLeaseError()
        if operation == "create" and schema == NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA:
            if set(wire) != _NLE_AUTHORING_ASSET_CREATE_KEYS:
                raise MediaLeaseError()
            return CreateLeaseRequest(
                request_id=wire["requestId"],
                workspace_handle=wire["workspaceHandle"],
                workspace_revision=wire["workspaceRevision"],
                timeline_revision=wire["timelineRevision"],
                public_fingerprint=None,
                manifest_fingerprint=wire["manifestFingerprint"],
                profile_fingerprint=wire["profileFingerprint"],
                scope="asset",
                clip_id=None,
                asset_id=wire["assetId"],
                derivative_kind=wire["derivativeKind"],
                owner_id=wire["ownerId"],
                runtime_epoch=wire["runtimeEpoch"],
                source_start_frame=wire["sourceStartFrame"],
                source_end_frame=wire["sourceEndFrame"],
                request_schema=NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA,
                authoring_schema=wire["authoringSchema"],
                authoring_profile_id=wire["profileId"],
                authoring_fingerprint=wire["authoringFingerprint"],
            )
        if operation == "create":
            if schema != REQUEST_SCHEMA:
                raise MediaLeaseError()
            if set(wire) != _CREATE_KEYS:
                raise MediaLeaseError()
            return CreateLeaseRequest(
                wire["requestId"],
                wire["workspaceHandle"],
                wire["workspaceRevision"],
                wire["timelineRevision"],
                wire["publicFingerprint"],
                wire["manifestFingerprint"],
                wire["profileFingerprint"],
                wire["scope"],
                wire["clipId"],
                wire["assetId"],
                wire["derivativeKind"],
                wire["ownerId"],
                wire["runtimeEpoch"],
                wire["sourceStartFrame"],
                wire["sourceEndFrame"],
            )
        expected = _COMMAND_KEYS | (
            {"nextOwnerId", "nextRuntimeEpoch"} if wire.get("operation") == "transfer" else set()
        )
        if schema != REQUEST_SCHEMA or set(wire) != expected:
            raise MediaLeaseError()
        return LeaseCommand(
            wire["operation"],
            wire["requestId"],
            wire["leaseId"],
            wire["revision"],
            wire["ownerId"],
            wire["runtimeEpoch"],
            wire.get("nextOwnerId"),
            wire.get("nextRuntimeEpoch"),
        )
    except (ValueError, UnicodeError, RecursionError, TypeError, KeyError) as exc:
        if isinstance(exc, MediaLeaseError):
            raise
        raise MediaLeaseError() from None


def public_runtime_asset_wire(asset: PublicAsset) -> dict[str, object]:
    return {
        "assetId": asset.asset_id,
        "kind": asset.kind,
        "sourceTimeBase": None
        if asset.source_time_base is None
        else asset.source_time_base.to_wire(),
        "sourceFrameCount": asset.source_frame_count,
        "sourceSampleCount": asset.source_sample_count,
        "embeddedAudio": asset.embedded_audio,
        "timestampPolicy": asset.timestamp_policy,
        "landmarks": [
            {
                "frameIndex": row.frame_index,
                "pts": row.pts,
                "dts": row.dts,
                "durationTicks": row.duration_ticks,
            }
            for row in asset.landmarks
        ],
    }


def public_asset_manifest_fingerprint(snapshot: PublicCompositionSnapshot) -> str:
    return composition_contract_fingerprint(
        {
            "schema": "h3.context.public_media_asset_manifest.v1",
            "profileId": ENGINE_PROFILE_ID,
            "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
            "publicFingerprint": snapshot.public_fingerprint,
            "workspaceRevision": snapshot.workspace_revision,
            "timelineRevision": snapshot.timeline_revision,
            "assets": [public_runtime_asset_wire(asset) for asset in snapshot.assets],
        }
    )


def validate_lease_snapshot(
    request: CreateLeaseRequest, snapshot: PublicCompositionSnapshot
) -> PublicAsset:
    if (
        request.workspace_handle != snapshot.workspace_handle
        or request.workspace_revision != snapshot.workspace_revision
        or request.timeline_revision != snapshot.timeline_revision
        or request.public_fingerprint != snapshot.public_fingerprint
        or request.manifest_fingerprint != public_asset_manifest_fingerprint(snapshot)
    ):
        raise MediaLeaseError("stale")
    asset = next((item for item in snapshot.assets if item.asset_id == request.asset_id), None)
    if asset is None:
        raise MediaLeaseError("authority_mismatch")
    if request.scope == "asset":
        if asset.kind == "font":
            raise MediaLeaseError("unsupported")
        if request.derivative_kind not in {"thumbnail", "filmstrip", "audio_peaks"}:
            raise MediaLeaseError("unsupported")
        if request.derivative_kind in {"filmstrip", "audio_peaks"}:
            if (
                asset.kind != "video"
                or asset.source_frame_count != len(asset.landmarks)
                or not 1 <= len(asset.landmarks) <= MAX_SOURCE_FRAMES
            ):
                raise MediaLeaseError("unsupported")
            if request.derivative_kind == "audio_peaks" and (
                asset.embedded_audio != "present_bound" or asset.source_sample_count is None
            ):
                raise MediaLeaseError("unsupported")
            span = (0, asset.source_frame_count)
        else:
            span = (0, 1)
        if (request.source_start_frame, request.source_end_frame) != span:
            raise MediaLeaseError("authority_mismatch")
        return asset
    clip = next((item for item in snapshot.clips if item.clip_id == request.clip_id), None)
    if clip is None:
        raise MediaLeaseError("authority_mismatch")
    font_id = None if clip.text is None else clip.text.font_asset_id
    if (asset.kind == "font" and font_id != asset.asset_id) or (
        asset.kind != "font" and clip.asset_id != asset.asset_id
    ):
        raise MediaLeaseError("authority_mismatch")
    allowed = {
        "video": ("video_proxy", "audio_preview", "frame_timing_index", "thumbnail"),
        "image": ("image_proxy", "thumbnail"),
        "font": ("packaged_font_face",),
    }
    if request.derivative_kind not in allowed.get(asset.kind, ()):
        raise MediaLeaseError("unsupported")
    if request.derivative_kind == "audio_preview":
        track = next((row for row in snapshot.tracks if row.track_id == clip.track_id), None)
        # CRITICAL: audio preview authority is primary-only. Admitting an overlay here would let
        # browser ownership multiply audible leases outside the producer-selected audio policy.
        if (
            track is None
            or track.kind != "primary_video"
            or not track.enabled
            or not clip.enabled
            or asset.embedded_audio != "present_bound"
            or asset.source_sample_count is None
        ):
            raise MediaLeaseError("unsupported")
    span = (0, 1)
    if asset.kind == "video":
        if (
            asset.source_frame_count != len(asset.landmarks)
            or not 1 <= len(asset.landmarks) <= MAX_SOURCE_FRAMES
        ):
            raise MediaLeaseError("unsupported")
        span = (
            (clip.source_start_frame, clip.source_start_frame + 1)
            if request.derivative_kind == "thumbnail"
            else (0, asset.source_frame_count)
        )
    if (request.source_start_frame, request.source_end_frame) != span:
        raise MediaLeaseError("authority_mismatch")
    return asset


@dataclass(frozen=True, slots=True)
class MediaGeometry:
    """Content-free original and derivative pixel geometry for a visual lease."""

    source_width: int
    source_height: int
    derivative_width: int
    derivative_height: int

    def __post_init__(self) -> None:
        for value in (
            self.source_width,
            self.source_height,
            self.derivative_width,
            self.derivative_height,
        ):
            require_integer(value, 1, MAX_MEDIA_GEOMETRY_EDGE)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": "h3.authoring.media_geometry.v1",
            "sourceWidth": self.source_width,
            "sourceHeight": self.source_height,
            "derivativeWidth": self.derivative_width,
            "derivativeHeight": self.derivative_height,
        }


@dataclass(frozen=True, slots=True)
class VerifiedDerivative:
    """Backend-only validated facts composed with the accepted manifest contracts."""

    source: PrivateSourceManifest
    derivative: DerivativeManifest
    kind: DerivativeKind
    asset_fingerprint: str
    generator_fingerprint: str
    byte_count: int
    audio_disposition: AudioDisposition
    geometry: MediaGeometry | None = None

    def __post_init__(self) -> None:
        if (
            type(self.source) is not PrivateSourceManifest
            or type(self.derivative) is not DerivativeManifest
        ):
            raise MediaLeaseError("internal_failure")
        if self.kind not in KINDS or self.audio_disposition not in AUDIO_DISPOSITIONS:
            raise MediaLeaseError("unsupported")
        if (
            self.source.asset_id != self.derivative.asset_id
            or self.source.source_fingerprint != self.derivative.original_fingerprint
            or self.source.generation != self.derivative.generation
            or self.derivative.generator_profile_id != DERIVATIVE_PROFILE_ID
        ):
            raise MediaLeaseError("stale")
        require_fingerprint(self.asset_fingerprint)
        require_fingerprint(self.generator_fingerprint)
        # IMPORTANT (M25-45): a body that simply does not fit the profile is a resource limit, at
        # every boundary that decides it -- this contract, the lease authority's own admission and
        # the browser's bounded read. `require_integer` would answer `invalid_request`, which reads
        # as "the caller sent something malformed" and is indistinguishable from a wrong field.
        # Keep malformed metadata on `invalid_request` and physical overflow here.
        require_integer(self.byte_count, 1)
        if self.byte_count > derivative_byte_limit(self.kind):
            raise MediaLeaseError("resource_limit")
        if self.geometry is not None:
            if type(self.geometry) is not MediaGeometry or self.kind not in {
                "video_proxy",
                "image_proxy",
                "thumbnail",
                "filmstrip",
            }:
                raise MediaLeaseError("unsupported")
            if (
                self.kind == "video_proxy"
                and max(self.geometry.derivative_width, self.geometry.derivative_height) > 1280
            ):
                raise MediaLeaseError("resource_limit")
        if self.kind in {"audio_peaks", "audio_preview"}:
            if self.audio_disposition != "present_bound" or self.geometry is not None:
                raise MediaLeaseError("unsupported")
        elif self.kind != "video_proxy" and self.audio_disposition != "absent":
            raise MediaLeaseError("unsupported")


__all__ = [
    "NLE_AUTHORING_ASSET_LEASE_REQUEST_SCHEMA",
    "CreateLeaseRequest",
    "LeaseCommand",
    "MediaGeometry",
    "MediaLeaseError",
    "VerifiedDerivative",
    "decode_lease_request",
    "public_asset_manifest_fingerprint",
    "public_runtime_asset_wire",
    "validate_lease_snapshot",
]
