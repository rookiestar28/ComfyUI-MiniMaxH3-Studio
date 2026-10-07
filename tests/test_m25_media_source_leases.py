from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from typing import cast

import pytest
from test_m25_media_derivative_contract import create_wire

from comfyui_h3_context.adapters.authoring_media_leases import MediaLeaseAuthority
from comfyui_h3_context.core.authoring_media import (
    DERIVATIVE_PROFILE_ID,
    CreateLeaseRequest,
    LeaseCommand,
    MediaGeometry,
    MediaLeaseError,
    Operation,
    VerifiedDerivative,
)
from comfyui_h3_context.core.composition_contract import (
    DERIVATIVE_MANIFEST_SCHEMA,
    PRIVATE_SOURCE_MANIFEST_SCHEMA,
    DerivativeManifest,
    PrivateSourceManifest,
)


class Claim:
    def __init__(self) -> None:
        self.live = True
        self.builds = 0
        self.cache_key = "private-test-cache-key"

    def current(self) -> bool:
        return self.live

    def confirm(self, _deadline: float) -> None:
        if not self.live:
            raise MediaLeaseError("stale")

    def generate(
        self, _deadline: float, _cancellation: object
    ) -> tuple[bytearray, VerifiedDerivative]:
        self.builds += 1
        body = bytearray(b"synthetic-state-machine-body")
        fp = "sha256:" + "1" * 64
        facts = VerifiedDerivative(
            PrivateSourceManifest(PRIVATE_SOURCE_MANIFEST_SCHEMA, "asset-1", 1, fp, "authority-1"),
            DerivativeManifest(
                DERIVATIVE_MANIFEST_SCHEMA,
                "asset-1",
                fp,
                DERIVATIVE_PROFILE_ID,
                "sha256:" + hashlib.sha256(body).hexdigest(),
                1,
            ),
            "video_proxy",
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            len(body),
            "absent",
        )
        return body, facts


Authority = tuple[MediaLeaseAuthority, Claim, list[float]]


def request(owner: str = "clip-1") -> CreateLeaseRequest:
    import json

    from comfyui_h3_context.core.authoring_media import decode_lease_request

    wire = create_wire()
    wire["ownerId"] = owner
    wire["requestId"] = "request-" + owner
    value = decode_lease_request(json.dumps(wire).encode())
    assert isinstance(value, CreateLeaseRequest)
    return value


def test_create_replay_rehashes_source_before_returning_authority(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, claim, _ = authority
    store.create(request())

    def changed_bytes(_deadline: float) -> None:
        raise MediaLeaseError("stale")

    monkeypatch.setattr(claim, "confirm", changed_bytes)
    # Cheap identity remains unchanged, modelling byte replacement with restored stat metadata.
    assert claim.current()
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(request())
    assert store.resources()["cacheBytes"] == 0


def test_failed_renewal_currentness_immediately_revokes_cached_body(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, claim, _ = authority
    receipt, cap = store.create(request())

    def changed_bytes(_deadline: float) -> None:
        raise MediaLeaseError("stale")

    monkeypatch.setattr(claim, "confirm", changed_bytes)
    with pytest.raises(MediaLeaseError, match="stale"):
        store.renew(command(receipt, "renew"), cap)
    assert store.resources()["cacheBytes"] == 0


def command(receipt: dict[str, object], operation: str, **kwargs: object) -> LeaseCommand:
    return LeaseCommand(
        cast(Operation, operation),
        "operation-1",
        cast(str, receipt["leaseId"]),
        cast(int, receipt["revision"]),
        cast(str, receipt["ownerId"]),
        cast(int, receipt["runtimeEpoch"]),
        cast(str | None, kwargs.get("next_owner_id")),
        cast(int | None, kwargs.get("next_runtime_epoch")),
    )


@pytest.fixture
def authority() -> Iterator[Authority]:
    now = [100.0]
    claim = Claim()
    store = MediaLeaseAuthority(lambda _request: claim, clock=lambda: now[0], start_reaper=False)
    yield store, claim, now
    store.close()
    assert store.resources() == {"leases": 0, "cacheEntries": 0, "cacheBytes": 0, "activeReads": 0}


def test_open_is_one_shot_per_owner_but_lease_remains_renewable(authority: Authority) -> None:
    store, _, _ = authority
    receipt, capability = store.create(request())
    body = store.open(command(receipt, "open"), capability)
    assert body.read_chunk() == b"synthetic-state-machine-body"
    body.discard()
    assert store.resources()["leases"] == 1
    with pytest.raises(MediaLeaseError, match="lease_gone"):
        store.open(command(receipt, "open"), capability)
    renewed = store.renew(command(receipt, "renew"), capability)
    assert renewed["revision"] == 2
    store.release(command(renewed, "release"), capability)
    store.release(command(renewed, "release"), capability)
    assert store.resources()["cacheBytes"] == 0


def test_geometry_metadata_is_capability_fenced_and_revoked_on_release() -> None:
    class GeometryClaim(Claim):
        def generate(
            self, deadline: float, cancellation: object
        ) -> tuple[bytearray, VerifiedDerivative]:
            body, facts = super().generate(deadline, cancellation)
            return body, replace(facts, geometry=MediaGeometry(1920, 1080, 320, 180))

    claim = GeometryClaim()
    store = MediaLeaseAuthority(lambda _: claim, start_reaper=False)
    try:
        receipt, capability = store.create(request())
        assert "geometry" not in receipt
        opened = command(receipt, "open")
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            store.metadata(opened, "0" * 64)
        read = store.open(opened, capability)
        assert store.metadata(opened, capability)["geometry"] == {
            "schema": "h3.authoring.media_geometry.v1",
            "sourceWidth": 1920,
            "sourceHeight": 1080,
            "derivativeWidth": 320,
            "derivativeHeight": 180,
        }
        store.release(command(receipt, "release"), capability)
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            read.read_chunk()
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            store.metadata(opened, capability)
    finally:
        store.close()


def test_video_proxy_geometry_rejects_derivative_edge_above_profile_limit() -> None:
    body, facts = Claim().generate(0.0, None)
    try:
        # M25-45 moved the proxy edge to 1280; the admission boundary refuses one pixel past it,
        # and accepts the edge itself, so the limit is proven rather than merely raised.
        replace(facts, geometry=MediaGeometry(1920, 1080, 1280, 720))
        with pytest.raises(MediaLeaseError, match="resource_limit"):
            replace(facts, geometry=MediaGeometry(1920, 1080, 1281, 720))
    finally:
        body.clear()


def test_transfer_rotates_authority_and_old_owner_cannot_release_new(authority: Authority) -> None:
    store, _, _ = authority
    old, old_cap = store.create(request())
    new, new_cap = store.transfer(
        command(old, "transfer", next_owner_id="clip-2", next_runtime_epoch=2), old_cap
    )
    assert new_cap != old_cap
    with pytest.raises(MediaLeaseError, match="lease_gone"):
        store.release(command(old, "release"), old_cap)
    assert store.resources()["leases"] == 1
    body = store.open(command(new, "open"), new_cap)
    assert body.read_chunk()
    body.discard()


def test_concurrent_read_blocks_transfer_and_renew(authority: Authority) -> None:
    store, _, _ = authority
    receipt, cap = store.create(request())
    body = store.open(command(receipt, "open"), cap)
    with pytest.raises(MediaLeaseError, match="busy"):
        store.renew(command(receipt, "renew"), cap)
    with pytest.raises(MediaLeaseError, match="busy"):
        store.transfer(
            command(receipt, "transfer", next_owner_id="other", next_runtime_epoch=2), cap
        )
    body.discard()


def test_expiry_clears_unfinished_read_and_cache(authority: Authority) -> None:
    store, _, now = authority
    receipt, cap = store.create(request())
    body = store.open(command(receipt, "open"), cap)
    now[0] += 61
    store.reap()
    with pytest.raises(MediaLeaseError):
        body.read_chunk()
    assert store.resources()["cacheBytes"] == 0


def test_semantic_source_change_revokes_before_read(authority: Authority) -> None:
    store, claim, _ = authority
    receipt, cap = store.create(request())
    claim.live = False
    with pytest.raises(MediaLeaseError):
        store.open(command(receipt, "open"), cap)
    assert store.resources()["leases"] == 0


def test_identical_source_derivative_is_shared_only_while_leased(authority: Authority) -> None:
    store, claim, _ = authority
    first, cap1 = store.create(request())
    second, cap2 = store.create(request("second"))
    assert claim.builds == 1
    store.release(command(first, "release"), cap1)
    assert store.resources()["cacheEntries"] == 1
    store.release(command(second, "release"), cap2)
    assert store.resources()["cacheEntries"] == 0
    store.create(request("third"))
    assert claim.builds == 2


def test_video_workspace_capacity_rejects_before_generation(authority: Authority) -> None:
    store, claim, _ = authority
    for owner in ("one", "two", "three"):
        store.create(request(owner))
    with pytest.raises(MediaLeaseError, match="resource_limit"):
        store.create(request("four"))
    assert claim.builds == 1


def test_wrong_capability_owner_and_revision_cannot_read(authority: Authority) -> None:
    store, _, _ = authority
    receipt, cap = store.create(request())
    good = command(receipt, "open")
    for value, token in (
        (good, "0" * 64),
        (replace(good, owner_id="foreign"), cap),
        (replace(good, revision=2), cap),
    ):
        with pytest.raises(MediaLeaseError, match="lease_gone"):
            store.open(value, token)


def test_late_source_change_discards_unpublished_generation(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, claim, _ = authority
    original = claim.generate

    def changed(deadline: float, cancellation: object) -> tuple[bytearray, VerifiedDerivative]:
        result = original(deadline, cancellation)
        claim.live = False
        return result

    monkeypatch.setattr(claim, "generate", changed)
    with pytest.raises(MediaLeaseError, match="stale"):
        store.create(request())
    assert store.resources()["cacheEntries"] == 0


def test_create_replay_is_stable_and_cannot_resurrect_a_released_lease(
    authority: Authority,
) -> None:
    store, claim, _ = authority
    original = store.create(request())
    assert store.create(request()) == original
    receipt, cap = original
    store.release(command(receipt, "release"), cap)
    with pytest.raises(MediaLeaseError, match="lease_gone"):
        store.create(request())
    assert claim.builds == 1


def test_renew_cannot_extend_absolute_source_lifetime(authority: Authority) -> None:
    store, _, now = authority
    receipt, cap = store.create(request())
    for _ in range(17):
        now[0] += 50
        receipt = store.renew(command(receipt, "renew"), cap)
    assert receipt["ttlMs"] == 50_000
    now[0] += 50
    with pytest.raises(MediaLeaseError, match="lease_gone"):
        store.renew(command(receipt, "renew"), cap)


def test_cancelled_create_never_calls_source_factory(authority: Authority) -> None:
    store, claim, _ = authority

    class Cancelled:
        def is_cancelled(self) -> bool:
            return True

    with pytest.raises(MediaLeaseError, match="cancelled"):
        store.create(request(), cancellation=Cancelled())
    assert claim.builds == 0


def test_close_during_generation_cannot_publish_or_retain_body(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, claim, _ = authority
    original = claim.generate
    captured = []

    def closed(deadline: float, cancellation: object) -> tuple[bytearray, VerifiedDerivative]:
        result = original(deadline, cancellation)
        captured.append(result[0])
        store.close()
        return result

    monkeypatch.setattr(claim, "generate", closed)
    with pytest.raises(MediaLeaseError, match="lease_gone"):
        store.create(request())
    assert captured == [bytearray()]
    assert store.resources()["cacheBytes"] == 0
