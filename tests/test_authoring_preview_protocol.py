from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

import pytest

from comfyui_h3_context.core.authoring_preview_protocol import (
    AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
    AUTHORING_PREVIEW_ERROR_SCHEMA,
    AUTHORING_PREVIEW_REQUEST_SCHEMA,
    AUTHORING_PREVIEW_SUCCESS_SCHEMA,
    MAX_AUTHORING_PREVIEW_REQUEST_BYTES,
    AuthoringPreviewCapability,
    AuthoringPreviewCapabilityReason,
    AuthoringPreviewError,
    AuthoringPreviewErrorReason,
    AuthoringPreviewRequest,
    AuthoringPreviewSuccess,
    decode_authoring_preview_capability,
    decode_authoring_preview_error,
    decode_authoring_preview_request,
    decode_authoring_preview_request_json,
    decode_authoring_preview_success,
    map_source_seconds_to_timeline,
    map_timeline_frame_to_source,
)
from comfyui_h3_context.core.contracts import MediaKind
from comfyui_h3_context.core.temporal_profile import TEMPORAL_PROFILE_SCHEMA
from comfyui_h3_context.core.timeline_authoring import TimelineClip, TimelineProfileInput

FP = "sha256:" + "a" * 64


def request_wire(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": AUTHORING_PREVIEW_REQUEST_SCHEMA,
        "requestId": "request-1",
        "workspaceHandle": "authoring-1",
        "referenceRevision": 4,
        "timelineRevision": 7,
        "timelineContentFingerprint": FP,
        "clipId": "clip-1",
    }
    value.update(overrides)
    return value


def clip() -> TimelineClip:
    return TimelineClip(
        clip_id="clip-1",
        asset_id="video-1",
        kind=MediaKind.VIDEO,
        lane=0,
        start_frame=100,
        frames=10,
        source_start_frame=10,
    )


def profile() -> TimelineProfileInput:
    return TimelineProfileInput(
        schema=TEMPORAL_PROFILE_SCHEMA,
        version=1,
        fingerprint=FP,
        video_fps=24,
        frame_grid=17,
        audio_period_frames=3,
        max_extent_frames=1_000_000,
    )


def test_closed_request_round_trip_uses_only_logical_identity() -> None:
    decoded = decode_authoring_preview_request(request_wire())

    assert isinstance(decoded, AuthoringPreviewRequest)
    assert decoded.to_wire() == request_wire()
    assert set(decoded.to_wire()) == {
        "schema",
        "requestId",
        "workspaceHandle",
        "referenceRevision",
        "timelineRevision",
        "timelineContentFingerprint",
        "clipId",
    }

    for private_key in ("path", "url", "bytes", "duration", "mime", "sourceRange"):
        with pytest.raises(ValueError, match="closed"):
            decode_authoring_preview_request(
                request_wire(**{private_key: "private"})  # pragma: allowlist secret
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "h3.context.authoring_source_preview.request.v2"},
        {"requestId": ""},
        {"workspaceHandle": "../private"},
        {"referenceRevision": True},
        {"referenceRevision": 0},
        {"timelineRevision": 1_000_001},
        {"timelineContentFingerprint": "sha256:nope"},
        {"clipId": ["clip-1"]},
    ],
)
def test_request_rejects_version_identity_and_bound_drift(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        decode_authoring_preview_request(request_wire(**overrides))


def test_json_decoder_rejects_duplicate_nonfinite_nested_and_oversized_input() -> None:
    assert (
        decode_authoring_preview_request_json(
            json.dumps(request_wire(), separators=(",", ":")).encode()
        ).request_id
        == "request-1"
    )

    duplicate = (
        b'{"schema":"'
        + AUTHORING_PREVIEW_REQUEST_SCHEMA.encode()
        + b'","requestId":"request-1","requestId":"request-2",'
        b'"workspaceHandle":"authoring-1","referenceRevision":4,"timelineRevision":7,'
        b'"timelineContentFingerprint":"' + FP.encode() + b'","clipId":"clip-1"}'
    )
    with pytest.raises(ValueError, match="repeats"):
        decode_authoring_preview_request_json(duplicate)
    with pytest.raises(ValueError):
        decode_authoring_preview_request_json(b'{"schema":NaN}')
    with pytest.raises(ValueError):
        decode_authoring_preview_request_json(
            json.dumps(request_wire(clipId={"nested": "clip-1"})).encode()
        )
    with pytest.raises(ValueError, match="bytes"):
        decode_authoring_preview_request_json(b"{" + b" " * MAX_AUTHORING_PREVIEW_REQUEST_BYTES)


def test_capability_success_and_error_vocabularies_are_closed_and_content_free() -> None:
    available = decode_authoring_preview_capability(
        {"schema": AUTHORING_PREVIEW_CAPABILITY_SCHEMA, "available": True, "reason": None}
    )
    unavailable = decode_authoring_preview_capability(
        {
            "schema": AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
            "available": False,
            "reason": "not_bound",
        }
    )
    assert available == AuthoringPreviewCapability(available=True)
    assert unavailable.reason is AuthoringPreviewCapabilityReason.NOT_BOUND
    with pytest.raises(ValueError):
        decode_authoring_preview_capability(
            {"schema": AUTHORING_PREVIEW_CAPABILITY_SCHEMA, "available": True, "reason": "stale"}
        )
    with pytest.raises(ValueError, match="closed"):
        decode_authoring_preview_capability(
            {
                "schema": AUTHORING_PREVIEW_CAPABILITY_SCHEMA,
                "available": False,
                "reason": "unavailable",
                "path": "private",
            }
        )

    success_wire = {
        **request_wire(),
        "schema": AUTHORING_PREVIEW_SUCCESS_SCHEMA,
        "sourceId": "video-1",
    }
    success = decode_authoring_preview_success(success_wire)
    assert isinstance(success, AuthoringPreviewSuccess)
    assert success.to_wire() == success_wire

    error = decode_authoring_preview_error(
        {"schema": AUTHORING_PREVIEW_ERROR_SCHEMA, "requestId": None, "reason": "invalid_request"}
    )
    assert error == AuthoringPreviewError(
        request_id=None,
        reason=AuthoringPreviewErrorReason.INVALID_REQUEST,
    )
    assert set(error.to_wire()) == {"schema", "requestId", "reason"}
    with pytest.raises(ValueError):
        decode_authoring_preview_error(
            {"schema": AUTHORING_PREVIEW_ERROR_SCHEMA, "requestId": None, "reason": "raw_stderr"}
        )


def test_forward_mapping_is_exact_and_preserves_half_open_clip_bounds() -> None:
    first = map_timeline_frame_to_source(clip(), profile(), 100)
    last = map_timeline_frame_to_source(clip(), profile(), 109)

    assert (first.timeline_frame, first.clip_local_frame, first.source_frame) == (100, 0, 10)
    assert first.source_seconds == Fraction(5, 12)
    assert (last.timeline_frame, last.clip_local_frame, last.source_frame) == (109, 9, 19)
    assert last.source_seconds == Fraction(19, 24)
    with pytest.raises(ValueError, match="half-open"):
        map_timeline_frame_to_source(clip(), profile(), 110)
    with pytest.raises(ValueError):
        map_timeline_frame_to_source(clip(), profile(), 99)


def test_inverse_mapping_rounds_half_up_once_and_clamps_to_included_frames() -> None:
    assert map_source_seconds_to_timeline(clip(), profile(), Fraction(10, 24)).timeline_frame == 100
    assert map_source_seconds_to_timeline(clip(), profile(), Fraction(19, 24)).timeline_frame == 109
    assert map_source_seconds_to_timeline(clip(), profile(), Fraction(7, 16)).source_frame == 11
    assert (
        map_source_seconds_to_timeline(clip(), profile(), Fraction(1049, 2400)).source_frame == 10
    )
    assert map_source_seconds_to_timeline(clip(), profile(), Fraction(0)).timeline_frame == 100
    assert map_source_seconds_to_timeline(clip(), profile(), Fraction(999)).timeline_frame == 109
    assert map_source_seconds_to_timeline(clip(), profile(), Decimal("0.4375")).source_frame == 11

    with pytest.raises(TypeError, match="exact"):
        map_source_seconds_to_timeline(clip(), profile(), cast(Any, 0.5))
    with pytest.raises(ValueError):
        map_source_seconds_to_timeline(clip(), profile(), Fraction(-1, 24))
    with pytest.raises(ValueError):
        map_source_seconds_to_timeline(clip(), profile(), Decimal("NaN"))


def test_schema_constants_are_exact_v1_contracts() -> None:
    assert AUTHORING_PREVIEW_REQUEST_SCHEMA == "h3.context.authoring_source_preview.request.v1"
    assert (
        AUTHORING_PREVIEW_CAPABILITY_SCHEMA == "h3.context.authoring_source_preview.capability.v1"
    )
    assert AUTHORING_PREVIEW_SUCCESS_SCHEMA == "h3.context.authoring_source_preview.success.v1"
    assert AUTHORING_PREVIEW_ERROR_SCHEMA == "h3.context.authoring_source_preview.error.v1"
