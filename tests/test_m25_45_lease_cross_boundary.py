"""M25-45: backend byte-capacity cases from the section 7 cross-boundary table.

The backend and the browser each admit a body on their own, and after the proxy migration they
admit a much larger one. These cases fix what each end does at the new edges: what it accepts, what
it refuses, and with which reason -- because a client that sees one physical condition reported two
different ways cannot tell "this source is too big for the profile" from "the encoder went wrong".

These synthetic bodies exercise authority/cache arithmetic only. They are not codec, playback,
presentation, dissolve or real-route evidence. The real-route exact-ceiling codec journey is
`frontend/tests/e2e/journeys/realMediaSourceLeases.spec.ts`; case (e) remains in
`tests/test_m25_45_derivative_table_parity.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Final, cast

import pytest
from test_m25_media_derivative_contract import create_wire

from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.core.authoring_media import (
    DERIVATIVE_PROFILE_ID,
    MAX_CACHE_BYTES,
    MAX_WORKSPACE_VIDEO_LEASES,
    CreateLeaseRequest,
    DerivativeKind,
    LeaseCommand,
    MediaLeaseError,
    VerifiedDerivative,
    decode_lease_request,
    derivative_byte_limit,
)
from comfyui_h3_context.core.composition_contract import (
    DERIVATIVE_MANIFEST_SCHEMA,
    PRIVATE_SOURCE_MANIFEST_SCHEMA,
    DerivativeManifest,
    PrivateSourceManifest,
)

#: The kinds this item did not touch, with the ceilings they had before it.
UNTOUCHED: Final[dict[DerivativeKind, int]] = {
    "frame_timing_index": 16 * 1024,
    "thumbnail": 512 * 1024,
    "image_proxy": 16 * 1024 * 1024,
    "packaged_font_face": 1024 * 1024,
}


class SizedClaim:
    """A byte-capacity claim; its repeated 0x7f body is deliberately not media."""

    def __init__(self, kind: DerivativeKind, byte_count: int, key: str = "") -> None:
        self.kind = kind
        self.byte_count = byte_count
        self.live = True
        # Two owners holding equal bodies of the same kind are two cache entries unless they are
        # genuinely the same private source; `key` is how a test says which of the two it means.
        self.cache_key = f"private-{kind}-{byte_count}-{key}"

    def current(self) -> bool:
        return self.live

    def confirm(self, _deadline: float) -> None:
        if not self.live:
            raise MediaLeaseError("stale")

    def generate(
        self, _deadline: float, _cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        body = bytearray(b"\x7f" * self.byte_count)
        source_fingerprint = "sha256:" + "1" * 64
        facts = VerifiedDerivative(
            PrivateSourceManifest(
                PRIVATE_SOURCE_MANIFEST_SCHEMA, "asset-1", 1, source_fingerprint, "authority-1"
            ),
            DerivativeManifest(
                DERIVATIVE_MANIFEST_SCHEMA,
                "asset-1",
                source_fingerprint,
                DERIVATIVE_PROFILE_ID,
                "sha256:" + hashlib.sha256(body).hexdigest(),
                1,
            ),
            self.kind,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            len(body),
            "absent",
        )
        return body, facts


def lease_request(kind: DerivativeKind, owner: str = "clip-1") -> CreateLeaseRequest:
    wire = create_wire()
    wire["derivativeKind"] = kind
    wire["ownerId"] = owner
    wire["requestId"] = f"request-{kind}-{owner}"
    value = decode_lease_request(json.dumps(wire).encode())
    assert isinstance(value, CreateLeaseRequest)
    return value


def open_command(receipt: dict[str, object]) -> LeaseCommand:
    return LeaseCommand(
        "open",
        "operation-1",
        cast(str, receipt["leaseId"]),
        cast(int, receipt["revision"]),
        cast(str, receipt["ownerId"]),
        cast(int, receipt["runtimeEpoch"]),
        None,
        None,
    )


def release_command(receipt: dict[str, object]) -> LeaseCommand:
    return LeaseCommand(
        "release",
        "operation-release",
        cast(str, receipt["leaseId"]),
        cast(int, receipt["revision"]),
        cast(str, receipt["ownerId"]),
        cast(int, receipt["runtimeEpoch"]),
        None,
        None,
    )


def authority_for(claim: SizedClaim) -> Iterator[MediaLeaseAuthority]:
    store = MediaLeaseAuthority(lambda _request: claim, clock=lambda: 100.0, start_reaper=False)
    try:
        yield store
    finally:
        store.close()


def test_a_proxy_body_above_the_old_ceiling_is_admitted_at_the_new_one() -> None:
    # The whole point of the migration: a body the pre-M25-45 backend would have refused now
    # publishes, so a 1280 px proxy can exist at all.
    byte_count = 12 * 1024 * 1024
    assert byte_count > 8 * 1024 * 1024
    claim = SizedClaim("video_proxy", byte_count)
    for store in authority_for(claim):
        receipt, capability = store.create(lease_request("video_proxy"))
        assert store.open(open_command(receipt), capability).read_chunk() is not None
        assert store.metadata(open_command(receipt), capability)["byteCount"] == byte_count
        assert store.resources()["cacheBytes"] == byte_count


@pytest.mark.parametrize("kind", ["video_proxy", *UNTOUCHED])
def test_one_byte_over_the_ceiling_is_a_resource_limit_not_a_generation_failure(
    kind: DerivativeKind,
) -> None:
    limit = derivative_byte_limit(kind)
    claim = SizedClaim(kind, limit + 1)
    for store in authority_for(claim):
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            store.create(lease_request(kind))
        # Nothing is published, cached or leaked by the refusal.
        assert store.resources() == {
            "leases": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "activeReads": 0,
        }


@pytest.mark.parametrize("kind", ["video_proxy", *UNTOUCHED])
def test_a_body_at_exactly_the_ceiling_is_admitted(kind: DerivativeKind) -> None:
    limit = derivative_byte_limit(kind)
    claim = SizedClaim(kind, limit)
    for store in authority_for(claim):
        receipt, capability = store.create(lease_request(kind))
        assert store.open(open_command(receipt), capability).read_chunk() is not None
        assert store.metadata(open_command(receipt), capability)["byteCount"] == limit


def test_the_four_untouched_kinds_keep_the_ceilings_they_had() -> None:
    for kind, ceiling in UNTOUCHED.items():
        assert derivative_byte_limit(kind) == ceiling


def test_an_unknown_derivative_kind_is_refused_before_anything_is_generated() -> None:
    wire = create_wire()
    wire["derivativeKind"] = "video_proxy_v2"
    with pytest.raises(Exception):  # noqa: B017 - the decoder's own closed refusal
        decode_lease_request(json.dumps(wire).encode())
    with pytest.raises(KeyError):
        derivative_byte_limit(cast(DerivativeKind, "video_proxy_v2"))


def authority_over(claims: dict[str, SizedClaim]) -> Iterator[MediaLeaseAuthority]:
    """One authority serving a different claim per owner, so several owners can hold at once."""

    store = MediaLeaseAuthority(
        lambda request: claims[request.owner_id], clock=lambda: 100.0, start_reaper=False
    )
    try:
        yield store
    finally:
        store.close()


def test_three_ceiling_proxies_two_images_and_headroom_hold_at_once() -> None:
    """The aggregate the ceilings were chosen for, executed rather than computed.

    `test_m25_45_derivative_table_parity` checks that the numbers add up. This checks that the
    authority actually admits them together: three video owners at the video ceiling -- the most
    one workspace may hold -- plus two image proxies at theirs and the index, thumbnail and font
    headroom beside them, all live at the same moment, published and readable.
    """

    video_limit = derivative_byte_limit("video_proxy")
    image_limit = derivative_byte_limit("image_proxy")
    plan: list[tuple[str, DerivativeKind, int]] = [
        ("clip-video-1", "video_proxy", video_limit),
        ("clip-video-2", "video_proxy", video_limit),
        ("clip-video-3", "video_proxy", video_limit),
        ("clip-image-1", "image_proxy", image_limit),
        ("clip-image-2", "image_proxy", image_limit),
        ("clip-index-1", "frame_timing_index", derivative_byte_limit("frame_timing_index")),
        ("clip-thumb-1", "thumbnail", derivative_byte_limit("thumbnail")),
        ("clip-font-1", "packaged_font_face", derivative_byte_limit("packaged_font_face")),
    ]
    claims = {owner: SizedClaim(kind, size, owner) for owner, kind, size in plan}
    expected = sum(size for _owner, _kind, size in plan)
    assert expected <= MAX_CACHE_BYTES
    for store in authority_over(claims):
        held: list[tuple[dict[str, object], str]] = []
        for owner, kind, size in plan:
            receipt, capability = store.create(lease_request(kind, owner))
            assert store.open(open_command(receipt), capability).read_chunk() is not None
            assert store.metadata(open_command(receipt), capability)["byteCount"] == size
            held.append((receipt, capability))
        resources = store.resources()
        assert resources["leases"] == len(plan)
        assert resources["cacheEntries"] == len(plan)
        assert resources["cacheBytes"] == expected
        # Nothing held after the composition closes: every owner releases, and the cache with it.
        for receipt, capability in held:
            store.release(release_command(receipt), capability)
        assert store.resources() == {
            "leases": 0,
            "cacheEntries": 0,
            "cacheBytes": 0,
            "activeReads": 0,
        }


def test_a_dissolve_between_two_ceiling_proxies_holds_both_without_a_cache_refusal() -> None:
    """The valid ceiling-to-ceiling dissolve: two assets at the video ceiling, both presented.

    A cross-dissolve reads two proxies at the same frame, so both bodies are live at once. At the
    migrated ceiling that is 48 MiB in one cache, which the aggregate admits -- and which a cache
    sized for the old ceiling would have refused mid-dissolve, with `resource_limit` arriving as a
    playback failure rather than as an admission decision.
    """

    limit = derivative_byte_limit("video_proxy")
    claims = {
        "clip-outgoing": SizedClaim("video_proxy", limit, "clip-outgoing"),
        "clip-incoming": SizedClaim("video_proxy", limit, "clip-incoming"),
    }
    for store in authority_over(claims):
        opened = []
        for owner in claims:
            receipt, capability = store.create(lease_request("video_proxy", owner))
            reader = store.open(open_command(receipt), capability)
            assert reader.read_chunk() is not None
            opened.append((receipt, capability))
        assert store.resources()["cacheBytes"] == 2 * limit
        assert store.resources()["leases"] == 2
        for receipt, capability in opened:
            store.release(release_command(receipt), capability)
        assert store.resources()["cacheBytes"] == 0


def test_a_fourth_video_owner_is_refused_before_anything_is_generated() -> None:
    """Three is the workspace's video ceiling; the fourth is a bounded refusal, not an eviction."""

    limit = derivative_byte_limit("video_proxy")
    owners = [f"clip-video-{index}" for index in range(1, 5)]
    claims = {owner: SizedClaim("video_proxy", limit, owner) for owner in owners}
    for store in authority_over(claims):
        for owner in owners[:MAX_WORKSPACE_VIDEO_LEASES]:
            receipt, capability = store.create(lease_request("video_proxy", owner))
            assert store.open(open_command(receipt), capability).read_chunk() is not None
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            store.create(lease_request("video_proxy", owners[MAX_WORKSPACE_VIDEO_LEASES]))
        # The refusal costs the three live owners nothing.
        resources = store.resources()
        assert resources["leases"] == MAX_WORKSPACE_VIDEO_LEASES
        assert resources["cacheBytes"] == MAX_WORKSPACE_VIDEO_LEASES * limit
