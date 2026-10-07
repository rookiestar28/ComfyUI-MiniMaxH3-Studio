"""M20-09 focused tests: receipt-codec injection over the private store."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_av_reconstruction_store import _policy
from test_av_seam_policy import _receipt as _seam_receipt_fixture

import comfyui_h3_context.adapters.av_reconstruction_store as store_module
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AV_RECONSTRUCTION_STORE_SCHEMA,
    AVStoreError,
    AVStoreInspectionStatus,
    AVStoreReceiptCodec,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.core.av_reconstruction import (
    AV_RECONSTRUCTION_RECEIPT_SCHEMA,
    AVOutputKind,
)
from comfyui_h3_context.core.av_seam_policy import (
    AV_SEAM_RECEIPT_SCHEMA,
    MAX_AV_SEAM_RECEIPT_BYTES,
    AVSeamReconstructionReceipt,
    decode_av_seam_reconstruction_receipt,
)
from comfyui_h3_context.core.canonical import canonical_bytes


def _seam_codec() -> AVStoreReceiptCodec:
    return AVStoreReceiptCodec(
        schema_id=AV_SEAM_RECEIPT_SCHEMA,
        receipt_type=AVSeamReconstructionReceipt,
        decode=decode_av_seam_reconstruction_receipt,
        max_receipt_bytes=MAX_AV_SEAM_RECEIPT_BYTES,
    )


def _payload_bound_receipt(payload: bytes) -> AVSeamReconstructionReceipt:
    receipt = _seam_receipt_fixture()
    return replace(
        receipt,
        outputs=(
            replace(
                receipt.outputs[0],
                byte_length=len(payload),
                content_fingerprint="sha256:" + hashlib.sha256(payload).hexdigest(),
            ),
        ),
        receipt_fingerprint=None,
    )


def test_codec_configuration_is_validated() -> None:
    with pytest.raises(AVStoreError, match="receipt_codec_configuration"):
        AVStoreReceiptCodec(
            schema_id="",
            receipt_type=AVSeamReconstructionReceipt,
            decode=decode_av_seam_reconstruction_receipt,
            max_receipt_bytes=MAX_AV_SEAM_RECEIPT_BYTES,
        )
    with pytest.raises(AVStoreError, match="receipt_codec_configuration"):
        AVStoreReceiptCodec(
            schema_id=AV_SEAM_RECEIPT_SCHEMA,
            receipt_type=AVSeamReconstructionReceipt,
            decode=decode_av_seam_reconstruction_receipt,
            max_receipt_bytes=0,
        )


def test_default_store_keeps_the_v1_codec_and_refuses_seam_receipts() -> None:
    payload = b"seam aggregate bytes"
    receipt = _payload_bound_receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "root",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        assert store.receipt_schema == AV_RECONSTRUCTION_RECEIPT_SCHEMA
        with pytest.raises(AVStoreError, match="receipt_type"):
            store.inspect(receipt)
        transaction = store.begin(
            transaction_id=receipt.transaction_id,
            plan_fingerprint=receipt.plan_fingerprint,
            approval_fingerprint=receipt.approval_fingerprint,
            expires_at_ms=50_000,
        )
        with pytest.raises(AVStoreError, match="commit_type"):
            store.commit(transaction, receipt, output_leases=())


def test_one_root_refuses_two_codecs() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "root"
        PrivateAVReconstructionStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        with pytest.raises(AVStoreError, match="store_codec_mismatch"):
            PrivateAVReconstructionStore(
                root,
                policy=_policy(),
                clock_ms=lambda: 100,
                receipt_codec=_seam_codec(),
            )


def test_seam_store_round_trip_commits_and_reloads_the_exact_receipt() -> None:
    payload = b"seam aggregate bytes"
    receipt = _payload_bound_receipt(payload)
    now = [100]
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "root",
            policy=_policy(),
            clock_ms=lambda: now[0],
            receipt_codec=_seam_codec(),
        )
        assert store.receipt_schema == AV_SEAM_RECEIPT_SCHEMA
        transaction = store.begin(
            transaction_id=receipt.transaction_id,
            plan_fingerprint=receipt.plan_fingerprint,
            approval_fingerprint=receipt.approval_fingerprint,
            expires_at_ms=50_000,
        )
        with store.claim_execution(transaction) as claim:
            lease = store.allocate_output(
                transaction,
                handle=receipt.outputs[0].handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                claim=claim,
            )
            lease.path.write_bytes(payload)
            now[0] = 600
            committed = store.commit(
                transaction,
                receipt,
                output_leases=((receipt.outputs[0].handle, lease),),
                claim=claim,
            )
        assert committed == receipt
        loaded = store.load_complete(
            transaction_id=receipt.transaction_id,
            plan_fingerprint=receipt.plan_fingerprint,
            approval_fingerprint=receipt.approval_fingerprint,
        )
        assert loaded == receipt
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        assert (
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=1024,
            )
            == payload
        )


def test_persisted_root_pins_its_codec_across_process_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M20-09 review finding 1: the on-disk marker pins the receipt codec, so a
    fresh process cannot reopen a root under the other codec and let recovery
    delete the first codec's committed receipts as undecodable."""

    with tempfile.TemporaryDirectory() as temporary:
        seam_root = Path(temporary) / "seam-root"
        PrivateAVReconstructionStore(
            seam_root,
            policy=_policy(),
            clock_ms=lambda: 100,
            receipt_codec=_seam_codec(),
        )
        # A fresh process has an empty coordinator map; only the marker remains.
        monkeypatch.setattr(store_module, "_COORDINATORS", {})
        with pytest.raises(AVStoreError, match="store_codec_mismatch"):
            PrivateAVReconstructionStore(
                seam_root,
                policy=_policy(),
                clock_ms=lambda: 100,
            )
        monkeypatch.setattr(store_module, "_COORDINATORS", {})
        reopened = PrivateAVReconstructionStore(
            seam_root,
            policy=_policy(),
            clock_ms=lambda: 100,
            receipt_codec=_seam_codec(),
        )
        assert reopened.receipt_schema == AV_SEAM_RECEIPT_SCHEMA

        v1_root = Path(temporary) / "v1-root"
        v1_store = PrivateAVReconstructionStore(
            v1_root,
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        assert (v1_root / ".h3-av-reconstruction-store-v1").read_bytes() == canonical_bytes(
            {
                "schema": AV_RECONSTRUCTION_STORE_SCHEMA,
                "policy": _policy().to_wire(),
            }
        )
        assert v1_store.receipt_schema == AV_RECONSTRUCTION_RECEIPT_SCHEMA
        monkeypatch.setattr(store_module, "_COORDINATORS", {})
        with pytest.raises(AVStoreError, match="store_codec_mismatch"):
            PrivateAVReconstructionStore(
                v1_root,
                policy=_policy(),
                clock_ms=lambda: 100,
                receipt_codec=_seam_codec(),
            )


def test_seam_store_fails_closed_on_undecodable_receipt_bytes() -> None:
    receipt = _payload_bound_receipt(b"seam aggregate bytes")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "root"
        store = PrivateAVReconstructionStore(
            root,
            policy=_policy(),
            clock_ms=lambda: 100,
            receipt_codec=_seam_codec(),
        )
        hostile = root / "receipts" / f"{receipt.transaction_id}.json"
        hostile.write_bytes(b'{"schema":"not-a-seam-receipt"}')
        with pytest.raises(AVStoreError, match="receipt_invalid"):
            store.load_complete(
                transaction_id=receipt.transaction_id,
                plan_fingerprint=receipt.plan_fingerprint,
                approval_fingerprint=receipt.approval_fingerprint,
            )
