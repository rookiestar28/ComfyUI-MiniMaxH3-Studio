"""M17-11 private reconstruction-store atomicity and integrity tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import comfyui_h3_context.adapters.av_reconstruction_store as av_store_module
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AVStoreError,
    AVStoreInspectionStatus,
    AVStorePolicy,
    AVStoreTransaction,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
from comfyui_h3_context.adapters.segment_artifact_store import ArtifactStoreError
from comfyui_h3_context.core.av_reconstruction import (
    AVOperation,
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionReceipt,
    AVStreamAccounting,
)
from comfyui_h3_context.core.errors import MediaProcessError


def _fp(label: str | bytes) -> str:
    payload = label if isinstance(label, bytes) else label.encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _policy(**overrides: int) -> AVStorePolicy:
    values = {
        "max_member_bytes": 1024 * 1024,
        "max_total_bytes": 4 * 1024 * 1024,
        "max_transactions": 3,
        "max_members_per_transaction": 65,
        "max_recovery_entries": 256,
        "max_concurrent_writes": 1,
        "transaction_ttl_ms": 60_000,
    }
    values.update(overrides)
    return AVStorePolicy(
        **values,
    )


def _receipt(
    payload: bytes,
    *,
    transaction_id: str = "transaction.m17.11",
    output_handle: str = "avout_0123456789abcdef0123456789abcdef",
) -> AVReconstructionReceipt:
    return AVReconstructionReceipt(
        transaction_id=transaction_id,
        plan_fingerprint=_fp("plan"),
        approval_fingerprint=_fp("approval"),
        selective_plan_fingerprint=_fp("selective"),
        generation_state_fingerprint=_fp("generation"),
        artifact_receipt_fingerprints=(_fp("artifact.receipt"),),
        boundary_receipt_fingerprints=(),
        capability_fingerprint=_fp("capability"),
        execution_fingerprint=_fp("execution"),
        segment_results=(
            AVReceiptSegmentResult(
                segment_id="segment.1",
                artifact_receipt_fingerprint=_fp("artifact.receipt"),
                operations=(AVOperation.PASSTHROUGH,),
                accounting=AVStreamAccounting(
                    decoded_frames=30,
                    carried_frames=30,
                    dropped_frames=0,
                    generated_frames=0,
                    emitted_frames=30,
                    decoded_samples=48_000,
                    carried_samples=48_000,
                    dropped_samples=0,
                    inserted_samples=0,
                    emitted_samples=48_000,
                ),
                output_start=AVRational(0, 1),
                output_end=AVRational(1, 1),
            ),
        ),
        outputs=(
            AVReceiptOutput(
                handle=output_handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=len(payload),
                content_fingerprint=_fp(payload),
                video_frame_count=30,
                audio_sample_count=48_000,
                duration=AVRational(1, 1),
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=100,
    )


def _begin(
    store: PrivateAVReconstructionStore,
    *,
    transaction_id: str = "transaction.m17.11",
    plan_fingerprint: str | None = None,
    approval_fingerprint: str | None = None,
) -> AVStoreTransaction:
    return store.begin(
        transaction_id=transaction_id,
        plan_fingerprint=plan_fingerprint or _fp("plan"),
        approval_fingerprint=approval_fingerprint or _fp("approval"),
        expires_at_ms=10_000,
    )


def test_receipt_last_commit_is_visible_verified_and_idempotent() -> None:
    payload = b"synthetic-reconstruction-output"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)

        assert store.inspect(receipt).status is AVStoreInspectionStatus.MISSING
        completed = store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, lease),),
        )

        assert completed == receipt
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        assert (
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=len(payload),
            )
            == payload
        )
        assert store.commit(transaction, receipt, output_leases=()) == receipt
        public = json.dumps(store.inspect(receipt).to_public_dict(), sort_keys=True).lower()
        assert "path" not in public
        assert "locator" not in public


def test_same_root_execution_claim_blocks_non_owner_mutation_and_owner_can_commit() -> None:
    payload = b"synthetic-claimed-reconstruction-output"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        owner = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        peer = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(owner)
        peer_transaction = _begin(peer)

        with owner.claim_execution(transaction) as claim:
            assert claim.completed_receipt is None
            lease = owner.allocate_output(
                transaction,
                handle=receipt.outputs[0].handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                claim=claim,
            )
            lease.path.write_bytes(payload)

            with pytest.raises(AVStoreError, match="transaction_in_progress"):
                peer.allocate_output(
                    peer_transaction,
                    handle="avout_peer00000000000000000000000000",
                    kind=AVOutputKind.RECONSTRUCTION_FULL,
                )
            with pytest.raises(AVStoreError, match="transaction_in_progress"):
                peer.abort(peer_transaction)
            with pytest.raises(AVStoreError, match="transaction_in_progress"):
                peer.commit(peer_transaction, receipt, output_leases=())
            with pytest.raises(AVStoreError, match="transaction_in_progress"):
                peer.recover()

            assert lease.path.read_bytes() == payload
            completed = owner.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
                claim=claim,
            )

        assert completed == receipt
        assert owner.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE


def test_execution_claim_owner_can_abort_failed_work() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)

        with store.claim_execution(transaction) as claim:
            assert claim.completed_receipt is None
            lease = store.allocate_output(
                transaction,
                handle="avout_abort0000000000000000000000000",
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                claim=claim,
            )
            lease.path.write_bytes(b"failed-owned-output")
            store.abort(transaction, claim=claim)

        assert tuple((root / "staging").iterdir()) == ()


def test_receipt_publication_failure_leaves_no_complete_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-failed-publication"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)

        def fail_receipt(_receipt: AVReconstructionReceipt) -> None:
            raise AVStoreError("injected_receipt_failure")

        monkeypatch.setattr(store, "_publish_receipt", fail_receipt)
        with pytest.raises(AVStoreError, match="injected_receipt_failure"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )

        assert store.inspect(receipt).status is AVStoreInspectionStatus.MISSING
        with pytest.raises(AVStoreError, match="reconstruction_not_complete"):
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=len(payload),
            )


def test_member_tamper_blocks_readback_and_exact_retry_conflict() -> None:
    payload = b"synthetic-original-output"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, lease),),
        )

        member = root / "members" / f"{receipt.outputs[0].handle}.bin"
        member.write_bytes(b"x" * len(payload))
        assert store.inspect(receipt).status is AVStoreInspectionStatus.TAMPERED
        with pytest.raises(AVStoreError, match="reconstruction_not_complete"):
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=len(payload),
            )

        conflicting = _receipt(payload + b"changed")
        with pytest.raises(AVStoreError, match="identity_conflict"):
            store.commit(transaction, conflicting, output_leases=())


def test_bounded_recovery_removes_only_owned_expired_staging_invalid_receipts_and_orphans() -> None:
    now = [100]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: now[0])
        transaction = store.begin(
            transaction_id="transaction.expired",
            plan_fingerprint=_fp("plan.expired"),
            approval_fingerprint=_fp("approval.expired"),
            expires_at_ms=200,
        )
        lease = store.allocate_output(
            transaction,
            handle="avout_expired000000000000000000000000",
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(b"expired-staging")
        (root / "receipts" / "transaction.invalid.json").write_bytes(b"{not-json")
        (root / "members" / "avout_orphan000000000000000000000000.bin").write_bytes(b"orphan")
        now[0] = 300

        report = store.recover()

        assert report.staging_removed == 1
        assert report.invalid_receipts_removed == 1
        assert report.orphan_members_removed == 1
        assert report.scanned_entries == 3
        assert tuple((root / "staging").iterdir()) == ()
        assert tuple((root / "receipts").iterdir()) == ()
        assert tuple((root / "members").iterdir()) == ()


def test_recovery_admits_entire_inventory_before_deleting_anything() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        orphan = root / "members" / "avout_orphan000000000000000000000000.bin"
        orphan.write_bytes(b"orphan")
        (root / "staging" / "foreign-entry").write_bytes(b"foreign")

        with pytest.raises(AVStoreError, match="unsafe_recovery_entry"):
            store.recover()

        assert orphan.read_bytes() == b"orphan"


def test_recovery_removes_only_valid_store_owned_state_temp() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        state_temp = root / "staging" / f"state.{'a' * 32}.tmp"
        state_temp.write_text(
            '{"enabled":false,"schema":"h3.context.av_reconstruction_store_state.v1"}',
            encoding="utf-8",
        )

        report = store.recover()

        assert report.scanned_entries == 1
        assert not state_temp.exists()


def test_same_root_policy_and_no_clobber_are_fail_closed() -> None:
    payload = b"synthetic-no-clobber"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        with pytest.raises(AVStoreError, match="store_policy_mismatch"):
            PrivateAVReconstructionStore(
                root,
                policy=AVStorePolicy(**{**_policy().to_wire(), "max_total_bytes": 5 * 1024 * 1024}),
                clock_ms=lambda: 100,
            )

        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        final = root / "members" / f"{receipt.outputs[0].handle}.bin"
        final.write_bytes(b"preexisting")
        with pytest.raises(AVStoreError, match="identity_conflict"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )
        assert final.read_bytes() == b"preexisting"
        assert store.inspect(receipt).status is AVStoreInspectionStatus.MISSING


def test_cleanup_failure_persists_disable_until_successful_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-cleanup-failure"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)

        def fail_receipt(_receipt: AVReconstructionReceipt) -> None:
            raise AVStoreError("injected_receipt_failure")

        original_unlink = cast(
            Callable[..., bool],
            av_store_module.__dict__["_safe_unlink"],
        )

        def fail_member_cleanup(
            path: Path,
            *,
            missing_ok: bool = True,
            maximum_links: int = 1,
        ) -> bool:
            if path.parent.name == "members":
                raise OSError("injected cleanup failure")
            return original_unlink(
                path,
                missing_ok=missing_ok,
                maximum_links=maximum_links,
            )

        monkeypatch.setattr(store, "_publish_receipt", fail_receipt)
        monkeypatch.setattr(av_store_module, "_safe_unlink", fail_member_cleanup)
        with pytest.raises(AVStoreError, match="cleanup_failed"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )

        state = json.loads((root / "store-state.json").read_text(encoding="utf-8"))
        assert state["enabled"] is False
        assert store.inspect(receipt).status is AVStoreInspectionStatus.DISABLED

        monkeypatch.setattr(av_store_module, "_safe_unlink", original_unlink)
        report = store.recover()
        assert report.orphan_members_removed == 1
        enabled = json.loads((root / "store-state.json").read_text(encoding="utf-8"))["enabled"]
        assert enabled is True


def test_post_receipt_cleanup_failure_disables_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-post-receipt-cleanup-failure"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        original_cleanup = store._cleanup_transaction

        def fail_cleanup(_transaction: AVStoreTransaction) -> None:
            raise AVStoreError("cleanup_failed")

        monkeypatch.setattr(store, "_cleanup_transaction", fail_cleanup)
        with pytest.raises(AVStoreError, match="cleanup_failed"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )

        state = json.loads((root / "store-state.json").read_text(encoding="utf-8"))
        assert state["enabled"] is False
        assert store.inspect(receipt).status is AVStoreInspectionStatus.DISABLED

        monkeypatch.setattr(store, "_cleanup_transaction", original_cleanup)
        assert store.recover().staging_removed == 1
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE


def test_active_transaction_and_allocation_limits_fail_before_growth() -> None:
    policy = _policy(
        max_transactions=1,
        max_members_per_transaction=1,
        max_recovery_entries=3,
    )
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "private-av-store",
            policy=policy,
            clock_ms=lambda: 100,
        )
        transaction = _begin(store)
        store.allocate_output(
            transaction,
            handle="avout_first0000000000000000000000000",
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )

        with pytest.raises(AVStoreError, match="member_count_limit"):
            store.allocate_output(
                transaction,
                handle="avout_second000000000000000000000000",
                kind=AVOutputKind.SEGMENT_EXPORT,
            )
        with pytest.raises(AVStoreError, match="transaction_quota_exceeded"):
            _begin(
                store,
                transaction_id="transaction.second",
                plan_fingerprint=_fp("plan.second"),
                approval_fingerprint=_fp("approval.second"),
            )


def test_hardlinked_inventory_blocks_new_publication() -> None:
    first_payload = b"synthetic-first-output"
    first_receipt = _receipt(first_payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        first_transaction = _begin(store)
        first_lease = store.allocate_output(
            first_transaction,
            handle=first_receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        first_lease.path.write_bytes(first_payload)
        store.commit(
            first_transaction,
            first_receipt,
            output_leases=((first_receipt.outputs[0].handle, first_lease),),
        )
        second_payload = b"synthetic-second-output"
        second_receipt = _receipt(
            second_payload,
            transaction_id="transaction.second",
            output_handle="avout_second000000000000000000000000",
        )
        second_transaction = _begin(
            store,
            transaction_id="transaction.second",
            plan_fingerprint=second_receipt.plan_fingerprint,
            approval_fingerprint=second_receipt.approval_fingerprint,
        )
        second_lease = store.allocate_output(
            second_transaction,
            handle=second_receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        second_lease.path.write_bytes(second_payload)
        first_member = root / "members" / f"{first_receipt.outputs[0].handle}.bin"
        hardlink = root / "members" / "avout_hardlink00000000000000000000000.bin"
        try:
            hardlink.hardlink_to(first_member)
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")

        with pytest.raises(AVStoreError, match="store_inventory_invalid"):
            store.commit(
                second_transaction,
                second_receipt,
                output_leases=((second_receipt.outputs[0].handle, second_lease),),
            )


def test_write_slot_and_byte_quota_are_shared_by_the_root() -> None:
    policy = _policy(max_member_bytes=32, max_total_bytes=32)
    first_payload = b"a" * 20
    second_payload = b"b" * 20
    first_receipt = _receipt(first_payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        first_store = PrivateAVReconstructionStore(root, policy=policy, clock_ms=lambda: 100)
        second_store = PrivateAVReconstructionStore(root, policy=policy, clock_ms=lambda: 100)
        first_transaction = _begin(first_store)
        first_lease = first_store.allocate_output(
            first_transaction,
            handle=first_receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        first_lease.path.write_bytes(first_payload)
        first_store.commit(
            first_transaction,
            first_receipt,
            output_leases=((first_receipt.outputs[0].handle, first_lease),),
        )

        second_receipt = _receipt(
            second_payload,
            transaction_id="transaction.second",
            output_handle="avout_second000000000000000000000000",
        )
        second_transaction = _begin(
            second_store,
            transaction_id="transaction.second",
            plan_fingerprint=second_receipt.plan_fingerprint,
            approval_fingerprint=second_receipt.approval_fingerprint,
        )
        second_lease = second_store.allocate_output(
            second_transaction,
            handle=second_receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        second_lease.path.write_bytes(second_payload)

        assert first_store._write_slots is second_store._write_slots
        assert first_store._write_slots.acquire(blocking=False)
        try:
            with pytest.raises(AVStoreError, match="write_concurrency_limit"):
                second_store.commit(
                    second_transaction,
                    second_receipt,
                    output_leases=((second_receipt.outputs[0].handle, second_lease),),
                )
        finally:
            first_store._write_slots.release()
        with pytest.raises(AVStoreError, match="byte_quota_exceeded"):
            second_store.commit(
                second_transaction,
                second_receipt,
                output_leases=((second_receipt.outputs[0].handle, second_lease),),
            )


def test_indirect_store_root_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        parent = Path(temporary)
        real_root = parent / "real-root"
        real_root.mkdir()
        indirect_root = parent / "indirect-root"
        try:
            indirect_root.symlink_to(real_root, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        with pytest.raises(AVStoreError, match="unsafe_store_root"):
            PrivateAVReconstructionStore(
                indirect_root,
                policy=_policy(),
                clock_ms=lambda: 100,
            )


def test_imported_cleanup_error_is_normalized_and_disables_after_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-imported-cleanup-error"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        original_unlink = cast(
            Callable[..., bool],
            av_store_module.__dict__["_safe_unlink"],
        )

        def fail_transaction_cleanup(
            path: Path,
            *,
            missing_ok: bool = True,
            maximum_links: int = 1,
        ) -> bool:
            if path.name == "transaction.json":
                raise ArtifactStoreError("unsafe_store_entry")
            return original_unlink(
                path,
                missing_ok=missing_ok,
                maximum_links=maximum_links,
            )

        monkeypatch.setattr(av_store_module, "_safe_unlink", fail_transaction_cleanup)
        with pytest.raises(AVStoreError, match="cleanup_failed"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )

        state = json.loads((root / "store-state.json").read_text(encoding="utf-8"))
        assert state["enabled"] is False
        assert store.inspect(receipt).status is AVStoreInspectionStatus.DISABLED


def test_hardlinked_receipt_inspection_is_closed_unsafe() -> None:
    payload = b"synthetic-hardlinked-receipt"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, lease),),
        )
        receipt_path = root / "receipts" / f"{receipt.transaction_id}.json"
        alias = root / "receipt-hardlink-alias.json"
        try:
            alias.hardlink_to(receipt_path)
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")

        assert store.inspect(receipt).status is AVStoreInspectionStatus.UNSAFE


def test_pre_receipt_lease_cleanup_failure_disables_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-lease-cleanup-failure"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        original_release = OwnedOutputLease.release

        def fail_release(self: OwnedOutputLease) -> None:
            if self is lease:
                raise MediaProcessError("injected lease cleanup failure")
            original_release(self)

        monkeypatch.setattr(OwnedOutputLease, "release", fail_release)
        with pytest.raises(AVStoreError, match="cleanup_failed"):
            store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
            )

        state = json.loads((root / "store-state.json").read_text(encoding="utf-8"))
        assert state["enabled"] is False
        assert tuple((root / "receipts").iterdir()) == ()


def test_duplicate_transaction_id_and_non_exact_retry_are_rejected() -> None:
    payload = b"synthetic-exact-retry"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=_policy(), clock_ms=lambda: 100)
        transaction = _begin(store)
        with pytest.raises(AVStoreError, match="identity"):
            _begin(
                store,
                plan_fingerprint=_fp("forged.plan"),
                approval_fingerprint=_fp("forged.approval"),
            )
        assert len(tuple((root / "staging").iterdir())) == 1

        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, lease),),
        )
        with pytest.raises(AVStoreError, match="identity"):
            forged = _begin(
                store,
                plan_fingerprint=_fp("forged.plan"),
                approval_fingerprint=_fp("forged.approval"),
            )
            store.commit(forged, receipt, output_leases=())


def test_active_exact_retry_returns_original_transaction_after_clock_advances() -> None:
    now = [100]
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        original = _begin(store)
        now[0] = 101

        retried = _begin(store)

        assert retried == original
        assert retried.created_at_ms == 100


def test_policy_and_recovery_reserve_one_owned_state_temp_entry() -> None:
    with pytest.raises(AVStoreError, match="policy_recovery_below_inventory"):
        _policy(
            max_transactions=1,
            max_members_per_transaction=1,
            max_recovery_entries=2,
        )

    policy = _policy(
        max_transactions=1,
        max_members_per_transaction=1,
        max_recovery_entries=3,
    )
    payload = b"synthetic-max-inventory"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(root, policy=policy, clock_ms=lambda: 100)
        transaction = _begin(store)
        lease = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, lease),),
        )
        state_temp = root / "staging" / f"state.{'b' * 32}.tmp"
        state_temp.write_text(
            '{"enabled":false,"schema":"h3.context.av_reconstruction_store_state.v1"}',
            encoding="utf-8",
        )

        assert store.recover().scanned_entries == 3
        assert not state_temp.exists()


def _committed(
    store: PrivateAVReconstructionStore, transaction_id: str, handle: str, payload: bytes
) -> AVReconstructionReceipt:
    receipt = _receipt(payload, transaction_id=transaction_id, output_handle=handle)
    transaction = _begin(store, transaction_id=transaction_id)
    lease = store.allocate_output(transaction, handle=handle, kind=AVOutputKind.RECONSTRUCTION_FULL)
    lease.path.write_bytes(payload)
    return cast(
        AVReconstructionReceipt,
        store.commit(transaction, receipt, output_leases=((handle, lease),)),
    )


def test_retirement_frees_quota_only_for_unreferenced_receipts_committed_before_the_snapshot() -> (
    None
):
    # Committed receipts count against the transaction quota for the life of the root, which
    # outlives every process-local owner; without retirement the store refuses all later work.
    now = [100]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "private-av-store"
        store = PrivateAVReconstructionStore(
            root, policy=_policy(max_transactions=2), clock_ms=lambda: now[0]
        )
        stale = _committed(store, "transaction.stale", "avout_stale0000000000000000000000000", b"s")
        live = _committed(store, "transaction.live", "avout_live00000000000000000000000000", b"l")
        with pytest.raises(AVStoreError, match="transaction_quota_exceeded"):
            _begin(store, transaction_id="transaction.next")

        # A receipt committed at or after the owner snapshot is never retired.
        assert (
            store.retire_unreferenced(
                keep_transaction_ids=frozenset({"transaction.live"}), completed_before_ms=100
            )
            == 0
        )
        assert store.inspect(stale).status is AVStoreInspectionStatus.COMPLETE

        now[0] = 200
        assert (
            store.retire_unreferenced(
                keep_transaction_ids=frozenset({"transaction.live"}), completed_before_ms=150
            )
            == 1
        )
        assert store.inspect(stale).status is AVStoreInspectionStatus.MISSING
        assert not (root / "members" / "avout_stale0000000000000000000000000.bin").exists()
        assert store.inspect(live).status is AVStoreInspectionStatus.COMPLETE
        assert _begin(store, transaction_id="transaction.next").transaction_id == (
            "transaction.next"
        )

        for keep, before in ((set(), 150), (frozenset(), 0), (frozenset(), True)):
            with pytest.raises(AVStoreError, match="retirement_invalid"):
                store.retire_unreferenced(
                    keep_transaction_ids=keep,  # type: ignore[arg-type]
                    completed_before_ms=before,
                )
