from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.managed_run_registry import (
    ManagedRunRegistryError,
    ManagedRunSlotReservationV1,
)
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactCapacityReservationV1,
    ArtifactStoreError,
    ArtifactStorePolicy,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
)


def _fingerprint(character: str) -> str:
    return "sha256:" + character * 64


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("reservation_id", "reservation with spaces"),
        ("parent_sequence_id", "parent\nsequence"),
        ("parent_authorization_fingerprint", "sha256:not-a-canonical-digest"),
        ("expires_at", float("nan")),
        ("expires_at", float("inf")),
    ),
)
def test_managed_run_slot_reservation_refuses_noncanonical_authority(
    override: str,
    value: object,
) -> None:
    fields: dict[str, object] = {
        "reservation_id": "reservation.1",
        "parent_sequence_id": "parent.1",
        "parent_authorization_fingerprint": _fingerprint("a"),
        "expires_at": 1_000.0,
    }
    fields[override] = value

    with pytest.raises(ManagedRunRegistryError, match="invalid managed run slot reservation"):
        ManagedRunSlotReservationV1(**fields)  # type: ignore[arg-type]


def _partial(artifact_id: str) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=artifact_id,
        state=ArtifactLifecycleState.PARTIAL,
        workspace_id="workspace.1",
        workspace_revision=1,
        workspace_fingerprint=_fingerprint("1"),
        segment_id="segment.1",
        manifest_fingerprint=_fingerprint("2"),
        producer_fingerprint=_fingerprint("3"),
        transaction_fingerprint=_fingerprint("4"),
        graph_fingerprint=_fingerprint("5"),
        native_binding_fingerprint=_fingerprint("6"),
        model_fingerprint=_fingerprint("7"),
        runtime_fingerprint=_fingerprint("8"),
        settings_fingerprint=_fingerprint("9"),
        source_id="source.1",
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fingerprint("a"),
        format_label="opaque.video.v1",
        shape=(1,),
        created_at_ms=100,
        expires_at_ms=1_000,
    )


def _policy(
    *,
    max_artifact_bytes: int,
    max_total_bytes: int,
    max_entries: int,
) -> ArtifactStorePolicy:
    return ArtifactStorePolicy(
        max_artifact_bytes=max_artifact_bytes,
        max_total_bytes=max_total_bytes,
        max_entries=max_entries,
        ttl_seconds=10,
        max_concurrent_writes=2,
        max_recovery_entries=max_entries * 2,
    )


def _reserve(
    store: PrivateSegmentArtifactStore,
    *,
    reservation_id: str = "reservation.1",
    segment_count: int = 2,
) -> ArtifactCapacityReservationV1:
    return store.reserve_capacity(
        reservation_id=reservation_id,
        parent_sequence_id="parent.1",
        parent_authorization_fingerprint=_fingerprint("b"),
        segment_count=segment_count,
        expires_at_ms=1_000,
    )


def test_fifteen_segment_reservation_uses_deterministic_equal_share() -> None:
    gibibyte = 1024 * 1024 * 1024
    max_artifact = 128 * 1024 * 1024
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(
                max_artifact_bytes=max_artifact,
                max_total_bytes=gibibyte,
                max_entries=64,
            ),
            clock_ms=lambda: 100,
        )

        reservation = _reserve(store, segment_count=15)

        assert reservation.per_artifact_max_bytes == gibibyte // 15
        assert reservation.per_artifact_max_bytes < max_artifact
        assert reservation.reserved_bytes == (gibibyte // 15) * 15
        assert reservation.reserved_entries == 15
        assert reservation.consumed_entries == 0
        assert reservation.consumed_bytes == 0
        assert not reservation.released


def test_reserved_entries_cannot_be_stolen_across_store_instances() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        policy = _policy(max_artifact_bytes=8, max_total_bytes=16, max_entries=2)
        owner = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        foreign = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        reservation = _reserve(owner)

        with pytest.raises(ArtifactStoreError, match="entry_quota_reserved"):
            foreign.begin(_partial("artifact.foreign"))

        owner.begin(_partial("artifact.owner"), capacity_reservation=reservation)
        observed = foreign.read_capacity_reservation(reservation)
        assert observed.consumed_entries == 1

        with pytest.raises(ArtifactStoreError, match="entry_quota_reserved"):
            foreign.begin(_partial("artifact.foreign"))

        released = owner.release_capacity_reservation(reservation)
        assert released.released
        foreign.begin(_partial("artifact.foreign"))


def test_reserved_bytes_are_atomic_and_release_keeps_committed_artifact() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        policy = _policy(max_artifact_bytes=8, max_total_bytes=16, max_entries=4)
        owner = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        foreign = PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100)
        reservation = _reserve(owner)
        foreign_partial = foreign.begin(_partial("artifact.foreign"))
        owner_partial = owner.begin(
            _partial("artifact.owner"),
            capacity_reservation=reservation,
        )

        with pytest.raises(ArtifactStoreError, match="byte_quota_reserved"):
            foreign.commit(foreign_partial, b"x")

        completed = owner.commit(
            owner_partial,
            b"data",
            capacity_reservation=reservation,
        )
        observed = foreign.read_capacity_reservation(reservation)
        assert observed.consumed_entries == 1
        assert observed.consumed_bytes == 4

        with pytest.raises(ArtifactStoreError, match="byte_quota_reserved"):
            foreign.commit(foreign_partial, b"x")

        owner.release_capacity_reservation(reservation)
        assert owner.read(completed) == b"data"
        assert foreign.commit(foreign_partial, b"x").byte_length == 1


def test_reservation_replay_conflict_cap_and_expiry_fail_closed() -> None:
    now = 100
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(max_artifact_bytes=12, max_total_bytes=16, max_entries=4),
            clock_ms=lambda: now,
        )
        reservation = _reserve(store)
        assert _reserve(store).fingerprint == reservation.fingerprint

        with pytest.raises(ArtifactStoreError, match="capacity_reservation_conflict"):
            _reserve(store, segment_count=1)

        partial = store.begin(
            _partial("artifact.owner"),
            capacity_reservation=reservation,
        )
        with pytest.raises(ArtifactStoreError, match="artifact_exceeds_reservation"):
            store.commit(partial, b"123456789", capacity_reservation=reservation)

        now = 1_001
        with pytest.raises(ArtifactStoreError, match="capacity_reservation_expired"):
            store.begin(
                _partial("artifact.expired"),
                capacity_reservation=reservation,
            )

        assert store.read_capacity_reservation(reservation).released


def test_concurrent_cross_instance_reservation_has_one_atomic_winner() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-store"
        policy = _policy(max_artifact_bytes=8, max_total_bytes=16, max_entries=2)
        stores = (
            PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100),
            PrivateSegmentArtifactStore(root, policy=policy, clock_ms=lambda: 100),
        )
        barrier = threading.Barrier(3)
        reservations: list[ArtifactCapacityReservationV1] = []
        failures: list[str] = []

        def compete(index: int) -> None:
            barrier.wait()
            try:
                reservations.append(_reserve(stores[index], reservation_id=f"reservation.{index}"))
            except ArtifactStoreError as exc:
                failures.append(exc.code)

        threads = [threading.Thread(target=compete, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert len(reservations) == 1
        assert failures == ["capacity_entries_unavailable"]


def test_exact_reusable_artifact_atomically_consumes_owned_capacity_once() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(max_artifact_bytes=8, max_total_bytes=24, max_entries=3),
            clock_ms=lambda: 100,
        )
        existing = store.commit(store.begin(_partial("artifact.reused")), b"data")
        reservation = _reserve(store)

        consumed = store.consume_reused_capacity(reservation, existing)

        assert consumed.consumed_entries == 1
        assert consumed.consumed_bytes == 4
        with pytest.raises(ArtifactStoreError, match="capacity_artifact_duplicate"):
            store.consume_reused_capacity(reservation, existing)
        assert store.read(existing) == b"data"


def test_missing_reuse_refuses_without_consuming_capacity() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateSegmentArtifactStore(
            Path(temporary) / "private-store",
            policy=_policy(max_artifact_bytes=8, max_total_bytes=24, max_entries=3),
            clock_ms=lambda: 100,
        )
        reservation = _reserve(store)
        missing = _partial("artifact.missing")

        with pytest.raises(ArtifactStoreError, match="artifact_not_reusable"):
            store.consume_reused_capacity(reservation, missing)

        unchanged = store.read_capacity_reservation(reservation)
        assert unchanged.consumed_entries == 0
        assert unchanged.consumed_bytes == 0
