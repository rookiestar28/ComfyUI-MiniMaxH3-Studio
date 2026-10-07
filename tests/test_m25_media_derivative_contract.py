from __future__ import annotations

import json

import pytest

from comfyui_h3_context.core.authoring_media import (
    REQUEST_SCHEMA,
    RUNTIME_PROFILE_FINGERPRINT,
    CreateLeaseRequest,
    LeaseCommand,
    MediaGeometry,
    MediaLeaseError,
    decode_lease_request,
)


def create_wire() -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "operation": "create",
        "requestId": "request-1",
        "workspaceHandle": "authoring-test",
        "workspaceRevision": 1,
        "timelineRevision": 1,
        "publicFingerprint": "sha256:" + "1" * 64,
        "manifestFingerprint": "sha256:" + "2" * 64,
        "profileFingerprint": RUNTIME_PROFILE_FINGERPRINT,
        "scope": "clip",
        "clipId": "clip-1",
        "assetId": "asset-1",
        "derivativeKind": "video_proxy",
        "ownerId": "clip-1",
        "runtimeEpoch": 1,
        "sourceStartFrame": 0,
        "sourceEndFrame": 12,
    }


def decode(wire: dict[str, object]) -> object:
    return decode_lease_request(json.dumps(wire).encode())


def test_create_round_trip_without_authority_or_private_source() -> None:
    value = decode(create_wire())
    assert isinstance(value, CreateLeaseRequest)
    assert value.to_wire() == create_wire()


@pytest.mark.parametrize("frames", [64, 65, 256, 257, 362, 512])
@pytest.mark.parametrize(
    "kind", ["video_proxy", "audio_preview", "frame_timing_index", "thumbnail"]
)
def test_complete_composition_source_span_reaches_lease_decoder(frames: int, kind: str) -> None:
    wire = create_wire()
    wire.update(derivativeKind=kind, sourceEndFrame=frames)
    if kind == "thumbnail":
        wire["sourceStartFrame"] = frames - 1
    value = decode(wire)
    assert isinstance(value, CreateLeaseRequest)
    assert value.to_wire() == wire


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("workspaceRevision", True),
        ("runtimeEpoch", -1),
        ("sourceEndFrame", 0),
        ("sourceStartFrame", 512),
        ("derivativeKind", "waveform"),
        ("derivativeKind", "font_subset"),
        ("profileFingerprint", "sha256:" + "0" * 64),
        ("assetId", "../private"),
        ("publicFingerprint", "private"),
        ("sourceEndFrame", 513),
    ],
)
def test_create_rejects_unsupported_or_unbounded_fields(key: str, value: object) -> None:
    wire = create_wire()
    wire[key] = value
    with pytest.raises(MediaLeaseError):
        decode(wire)


@pytest.mark.parametrize("key", ["path", "url", "capability", "token", "sourceFingerprint"])
def test_no_private_or_authorization_field_enters_json(key: str) -> None:
    wire = create_wire()
    wire[key] = "not-authority"
    with pytest.raises(MediaLeaseError, match="invalid_request"):
        decode(wire)


@pytest.mark.parametrize(
    "body", [b'{"operation":"create","operation":"open"}', b"[1]", b"{", b" " * 8193, b'{"x":NaN}']
)
def test_raw_json_is_closed_and_bounded(body: bytes) -> None:
    with pytest.raises(MediaLeaseError, match="invalid_request"):
        decode_lease_request(body)


def test_audio_preview_is_clip_scoped_and_requires_the_complete_source_span() -> None:
    wire = create_wire()
    wire["derivativeKind"] = "audio_preview"
    decoded = decode(wire)
    assert isinstance(decoded, CreateLeaseRequest)
    assert decoded.derivative_kind == "audio_preview"

    wire["scope"] = "asset"
    wire["clipId"] = None
    with pytest.raises(MediaLeaseError, match="unsupported"):
        decode(wire)


def test_transfer_requires_new_owner_and_epoch_and_rejects_extra_keys() -> None:
    wire: dict[str, object] = {
        "schema": REQUEST_SCHEMA,
        "operation": "transfer",
        "requestId": "r",
        "leaseId": "lease-1",
        "revision": 1,
        "ownerId": "old",
        "runtimeEpoch": 1,
        "nextOwnerId": "new",
        "nextRuntimeEpoch": 2,
    }
    decoded = decode(wire)
    assert isinstance(decoded, LeaseCommand)
    assert decoded.to_wire() == wire
    wire["nextRuntimeEpoch"] = True
    with pytest.raises(MediaLeaseError):
        decode(wire)


def test_media_geometry_is_closed_bounded_and_has_one_wire_shape() -> None:
    geometry = MediaGeometry(1920, 1080, 320, 180)
    assert geometry.to_wire() == {
        "schema": "h3.authoring.media_geometry.v1",
        "sourceWidth": 1920,
        "sourceHeight": 1080,
        "derivativeWidth": 320,
        "derivativeHeight": 180,
    }

    for values in (
        (True, 1080, 320, 180),
        (1920, 0, 320, 180),
        (1920, 1080, 16385, 180),
        (1920, 1080, 320, None),
    ):
        with pytest.raises(MediaLeaseError, match="invalid_request"):
            MediaGeometry(*values)
