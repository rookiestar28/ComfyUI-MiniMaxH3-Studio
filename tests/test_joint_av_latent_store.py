"""M20-04 focused tests for the private joint AV latent checkpoint store.

The store composes the accepted segment-artifact mechanics for a new receipt family, so the
suite proves the safety model holds for THIS store rather than assuming the composition: exact
extent admission, atomic no-clobber publication, tamper fail-closed inspection, quota/TTL and
same-root coordination, aggregate-before-mutation recovery, junction fail-closed behavior, and
locator-free projections.  Payloads are synthetic byte runs whose sizes come from the
descriptor's own integer arithmetic; no tensor library appears anywhere.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from official_temporal_parity import NATIVE_NODE_SHA256
from test_segment_artifact_store import _make_directory_link, _replace_with_directory_link

from comfyui_h3_context.adapters.joint_av_latent_store import (
    JOINT_AV_LATENT_STORE_SCHEMA,
    PrivateJointAVLatentStore,
)
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactInspectionStatus,
    ArtifactStoreError,
    ArtifactStorePolicy,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core.joint_av_latent import (
    JointAVLatentAuthority,
    JointAVLatentReceipt,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    decode_joint_av_latent_receipt,
)
from comfyui_h3_context.core.segment_artifacts import ArtifactLifecycleState
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)

#: Video (1, 24, 1, 1, 1) float16 -> 48 bytes; audio (1, 32, 2, 1) float16 -> 128 bytes.
_VIDEO_BYTES = 48
_AUDIO_BYTES = 128
_PAYLOAD = bytes(range(256))[: _VIDEO_BYTES + _AUDIO_BYTES]

_FP = "sha256:" + "ab" * 32


def _authority() -> JointAVLatentAuthority:
    accepted = AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=(
            QualifiedRow(
                row="joint_av_latent_descriptor",
                status=CapabilityStatus.SUPPORTED,
                reason_code="descriptor_measured_live",
                consumer="M20-04",
            ),
        ),
    )
    return build_joint_av_latent_authority(build_temporal_profile(accepted))


def _partial(*, artifact_id: str = "latent.1", now_ms: int = 100) -> JointAVLatentReceipt:
    authority = _authority()
    descriptor = build_joint_av_latent_descriptor(
        authority=authority,
        video_shape=(1, 24, 1, 1, 1),
        video_dtype="float16",
        audio_shape=(1, 32, 2, 1),
        audio_dtype="float16",
        requested_frames=1,
        completed_frames=1,
    )
    return begin_joint_av_latent_receipt(
        descriptor=descriptor,
        authority=authority,
        artifact_id=artifact_id,
        transaction_fingerprint=_FP,
        execution_fingerprint=_FP,
        model_fingerprint=_FP,
        runtime_fingerprint=_FP,
        settings_fingerprint=_FP,
        source_id="segment.1",
        predecessor_artifact_fingerprint=None,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + 1_000,
    )


def _policy(**overrides: int) -> ArtifactStorePolicy:
    values = {
        "max_artifact_bytes": 4096,
        "max_total_bytes": 8192,
        "max_entries": 8,
        "ttl_seconds": 10,
        "max_concurrent_writes": 1,
        "max_recovery_entries": 64,
    }
    values.update(overrides)
    return ArtifactStorePolicy(**values)


def _store(root: Path, *, clock: int = 100) -> PrivateJointAVLatentStore:
    return PrivateJointAVLatentStore(root, policy=_policy(), clock_ms=lambda: clock)


def test_lifecycle_round_trip_publishes_exactly_and_read_splits_domains() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        partial = store.begin(_partial())
        completed = store.commit(partial, _PAYLOAD)
        assert completed.state is ArtifactLifecycleState.COMPLETE
        assert completed.byte_length == len(_PAYLOAD)
        assert store.inspect(completed).status is ArtifactInspectionStatus.REUSABLE
        assert store.read(completed) == _PAYLOAD
        video, audio = store.read_domains(completed)
        assert video == _PAYLOAD[:_VIDEO_BYTES]
        assert audio == _PAYLOAD[_VIDEO_BYTES:]
        published = decode_joint_av_latent_receipt(
            (root / "receipts" / "latent.1.json").read_bytes()
        )
        assert published == completed


def test_commit_refuses_any_payload_that_is_not_the_descriptor_extent() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        store = _store(Path(temporary) / "latent-store")
        partial = store.begin(_partial())
        for wrong in (_PAYLOAD[:-1], _PAYLOAD + b"\x00"):
            with pytest.raises(ArtifactStoreError, match="payload_extent_mismatch"):
                store.commit(partial, wrong)
        # The exact payload still commits after the refusals: nothing was consumed.
        assert store.commit(partial, _PAYLOAD).state is ArtifactLifecycleState.COMPLETE


def test_wrong_identity_and_tampered_payload_are_never_reusable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        completed = store.commit(store.begin(_partial()), _PAYLOAD)
        foreign = replace(completed, source_id="segment.2", receipt_fingerprint=None)
        assert store.inspect(foreign).status is ArtifactInspectionStatus.INCOMPATIBLE
        latent_path = root / "latents" / "latent.1.bin"
        latent_path.write_bytes(b"\xff" + _PAYLOAD[1:])
        assert store.inspect(completed).status is ArtifactInspectionStatus.TAMPERED
        with pytest.raises(ArtifactStoreError, match="artifact_not_reusable"):
            store.read(completed)


def test_duplicate_ids_quota_ttl_and_concurrency_are_enforced() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        store.commit(store.begin(_partial()), _PAYLOAD)
        with pytest.raises(ArtifactStoreError, match="duplicate_artifact"):
            store.begin(_partial())
        expired = _partial(artifact_id="latent.expired", now_ms=99)
        late_store = PrivateJointAVLatentStore(root, policy=_policy(), clock_ms=lambda: 99 + 1_001)
        with pytest.raises(ArtifactStoreError, match="receipt_expired"):
            late_store.begin(expired)
        over_ttl = replace(
            _partial(artifact_id="latent.ttl"),
            expires_at_ms=100 + 11_000,
            receipt_fingerprint=None,
        )
        with pytest.raises(ArtifactStoreError, match="receipt_ttl_exceeds_policy"):
            store.begin(over_ttl)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-quota"
        quota_store = PrivateJointAVLatentStore(
            root,
            policy=_policy(max_artifact_bytes=200, max_total_bytes=300),
            clock_ms=lambda: 100,
        )
        quota_store.commit(quota_store.begin(_partial()), _PAYLOAD)
        with pytest.raises(ArtifactStoreError, match="byte_quota_exceeded"):
            quota_store.commit(quota_store.begin(_partial(artifact_id="latent.2")), _PAYLOAD)


def test_same_root_policy_drift_and_family_marker_are_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        _store(root)
        with pytest.raises(ArtifactStoreError, match="store_policy_mismatch"):
            PrivateJointAVLatentStore(
                root,
                policy=_policy(max_entries=4),
                clock_ms=lambda: 100,
            )

    with tempfile.TemporaryDirectory() as temporary:
        # A segment-artifact root is a different store family: the latent store must refuse
        # it rather than adopt it, whatever the policy agreement says.
        root = Path(temporary) / "segment-store"
        PrivateSegmentArtifactStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        with pytest.raises(ArtifactStoreError, match="foreign_store_root|unsupported_store_schema"):
            _store(root)


def test_receipt_publication_failure_rolls_back_payload_without_losing_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        partial = store.begin(_partial())
        original = PrivateJointAVLatentStore._atomic_publish_new

        def failing(self, final, payload, *, parent):  # type: ignore[no-untyped-def]
            if final.suffix == ".json" and final.parent.name == "receipts":
                raise ArtifactStoreError("store_publish_failed")
            return original(self, final, payload, parent=parent)

        monkeypatch.setattr(PrivateJointAVLatentStore, "_atomic_publish_new", failing)
        with pytest.raises(ArtifactStoreError, match="store_publish_failed"):
            store.commit(partial, _PAYLOAD)
        monkeypatch.setattr(PrivateJointAVLatentStore, "_atomic_publish_new", original)
        assert not (root / "latents" / "latent.1.bin").exists()
        assert (root / "staging" / "latent.1.partial.json").exists()
        assert store.commit(partial, _PAYLOAD).state is ArtifactLifecycleState.COMPLETE


def test_recovery_admits_aggregate_first_and_removes_only_owned_state() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        completed = store.commit(store.begin(_partial()), _PAYLOAD)
        store.begin(_partial(artifact_id="latent.partial"))
        (root / "latents" / "latent.orphan.bin").write_bytes(b"orphan")
        (root / "receipts" / "latent.broken.json").write_bytes(b"{not json")
        report = store.recover()
        assert report.staging_removed == 1
        assert report.orphans_removed == 1
        assert report.invalid_removed == 1
        assert store.inspect(completed).status is ArtifactInspectionStatus.REUSABLE

        # A foreign name anywhere in the inventory blocks recovery before any deletion.
        stray = root / "receipts" / "not a receipt!!"
        stray.write_bytes(b"x")
        survivor = root / "latents" / "latent.survivor.bin"
        survivor.write_bytes(b"would-be-orphan")
        with pytest.raises(ArtifactStoreError, match="unsafe_recovery_entry"):
            store.recover()
        assert survivor.exists()
        stray.unlink()
        survivor.unlink()


def test_expired_receipts_are_recovered_with_their_payloads() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        store.commit(store.begin(_partial()), _PAYLOAD)
        aged = PrivateJointAVLatentStore(root, policy=_policy(), clock_ms=lambda: 100 + 2_000)
        report = aged.recover()
        assert report.expired_removed == 1
        assert not (root / "latents" / "latent.1.bin").exists()
        assert not (root / "receipts" / "latent.1.json").exists()


def test_link_root_and_child_junction_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        target = base / "target"
        target.mkdir()
        link = base / "linked-store"
        _make_directory_link(link, target)
        with pytest.raises(ArtifactStoreError, match="unsafe_store_root"):
            _store(link)
        link.unlink() if link.is_symlink() else link.rmdir()

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "latent-store"
        store = _store(root)
        partial = store.begin(_partial())
        outside = base / "outside-latents"
        _replace_with_directory_link(root / "latents", outside)
        try:
            with pytest.raises(ArtifactStoreError, match="unsafe_store_entry"):
                store.commit(partial, _PAYLOAD)
            assert list(outside.iterdir()) == []
        finally:
            link = root / "latents"
            link.unlink() if link.is_symlink() else link.rmdir()


def test_disable_retain_and_purge_remove_only_store_owned_entries() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        completed = store.commit(store.begin(_partial()), _PAYLOAD)
        report = store.disable(purge=False)
        assert report.retained and not report.enabled
        assert store.inspect(completed).status is ArtifactInspectionStatus.DISABLED
        with pytest.raises(ArtifactStoreError, match="store_disabled"):
            store.begin(_partial(artifact_id="latent.2"))

        purged = store.disable(purge=True)
        assert purged.purged_entries == 1
        assert list((root / "latents").iterdir()) == []
        assert list((root / "receipts").iterdir()) == []


def test_stream_latent_into_copies_exactly_and_never_clobbers() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        store = _store(root)
        completed = store.commit(store.begin(_partial()), _PAYLOAD)
        destination = Path(temporary) / "resume" / "checkpoint.bin"
        destination.parent.mkdir()
        length, fingerprint = store.stream_latent_into(completed, destination)
        assert length == len(_PAYLOAD)
        assert fingerprint == "sha256:" + hashlib.sha256(_PAYLOAD).hexdigest()
        assert destination.read_bytes() == _PAYLOAD
        with pytest.raises(ArtifactStoreError):
            store.stream_latent_into(completed, destination)


def test_marker_binds_schema_and_policy() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "latent-store"
        _store(root)
        marker = (root / ".h3-joint-av-latent-store-v1").read_bytes()
        assert JOINT_AV_LATENT_STORE_SCHEMA.encode() in marker
