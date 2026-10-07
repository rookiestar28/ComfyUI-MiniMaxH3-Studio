"""M17-11 Phase D exact plan-to-private-receipt integration tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_av_reconstruction import _plan_kwargs
from test_av_reconstruction_media import (
    _accepted_pin,
    _executable_plan,
    _plan_from_inspected_descriptor,
    _qualified_binary_paths_from_environment,
    _ReplaySource,
)

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    AVMediaExecutionResult,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.av_reconstruction_pipeline import (
    AVReconstructionPipelineError,
    execute_av_reconstruction,
)
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AVStoreError,
    AVStoreInspectionStatus,
    AVStorePolicy,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
from comfyui_h3_context.adapters.segment_artifact_store import ArtifactStoreError
from comfyui_h3_context.core.av_reconstruction import (
    AVMediaDescriptor,
    AVOutputKind,
    AVPublicationState,
    AVReconstructionApproval,
    AVReconstructionPlan,
    AVReconstructionReceipt,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint


class _Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


def _fp(payload: str | bytes) -> str:
    raw = payload if isinstance(payload, bytes) else payload.encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _policy() -> AVStorePolicy:
    return AVStorePolicy(
        max_member_bytes=1024 * 1024,
        max_total_bytes=4 * 1024 * 1024,
        max_transactions=3,
        max_members_per_transaction=65,
        max_recovery_entries=256,
        max_concurrent_writes=1,
        transaction_ttl_ms=60_000,
    )


def _adapter(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock_ms: Callable[[], int],
) -> QualifiedAVMediaAdapter:
    ffmpeg = (root / "ffmpeg.exe").resolve()
    ffprobe = (root / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"synthetic ffmpeg")
    ffprobe.write_bytes(b"synthetic ffprobe")
    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
    return QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(root / "scratch").resolve(),
        clock_ms=clock_ms,
    )


def _output_descriptor(
    *,
    segment_id: str,
    authority_fingerprint: str,
    payload: bytes,
) -> AVMediaDescriptor:
    target = cast(AVMediaDescriptor, _plan_kwargs(count=1)["media_descriptors"][0])
    return replace(
        target,
        segment_id=segment_id,
        artifact_receipt_fingerprint=authority_fingerprint,
        artifact_output_fingerprint=_fp(payload),
        artifact_byte_length=len(payload),
        inspected_at_ms=400,
        expires_at_ms=60_400,
    )


def _successful_execution(
    *,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    segment_id: str,
    aggregate_payload: bytes,
    derived_payload: bytes | None,
) -> AVMediaExecutionResult:
    aggregate = _output_descriptor(
        segment_id="reconstruction.aggregate",
        authority_fingerprint=plan.fingerprint,
        payload=aggregate_payload,
    )
    derived = (
        ()
        if derived_payload is None
        else (
            _output_descriptor(
                segment_id=segment_id,
                authority_fingerprint=plan.fingerprint,
                payload=derived_payload,
            ),
        )
    )
    execution_fingerprint = canonical_fingerprint(
        {
            "plan_fingerprint": plan.fingerprint,
            "approval_fingerprint": approval.fingerprint,
            "capability_fingerprint": plan.capability.fingerprint,
            "aggregate": aggregate.to_wire(),
            "derived": [item.to_wire() for item in derived],
        }
    )
    return AVMediaExecutionResult(
        execution_fingerprint=execution_fingerprint,
        aggregate_descriptor=aggregate,
        derived_descriptors=derived,
    )


def test_pipeline_commits_exact_private_receipt_and_completed_retry_skips_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    derived_payload = b"synthetic-private-derived"
    plan, approval = _executable_plan(source_payload, missing_audio=True)
    now = [400]
    execute_calls = 0

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "private-av-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: now[0])

        def fake_execute(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            derived = cast(tuple[tuple[str, OwnedOutputLease], ...], kwargs["derived_outputs"])
            aggregate.path.write_bytes(aggregate_payload)
            assert len(derived) == 1
            derived[0][1].path.write_bytes(derived_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=derived_payload,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", fake_execute)
        receipt = execute_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.phase-d",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: now[0],
        )

        assert receipt.publication_state is AVPublicationState.COMPLETE
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        assert tuple(item.kind for item in receipt.outputs) == (
            AVOutputKind.RECONSTRUCTION_FULL,
            AVOutputKind.SEGMENT_EXPORT,
        )
        assert len(receipt.outputs) == 2
        assert all(item.handle.startswith("avout_") for item in receipt.outputs)
        assert all(len(item.handle) == len("avout_") + 64 for item in receipt.outputs)
        assert receipt.segment_results[0].derived_output_handle == receipt.outputs[1].handle
        assert (
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=len(aggregate_payload),
            )
            == aggregate_payload
        )
        assert (
            store.read_output(
                receipt,
                receipt.outputs[1].handle,
                maximum_bytes=len(derived_payload),
            )
            == derived_payload
        )
        public = json.dumps(receipt.to_public_dict(), sort_keys=True).lower()
        assert str(root).lower() not in public
        assert "ffmpeg" not in public
        assert "command" not in public
        with pytest.raises(AVStoreError, match="identity_conflict"):
            store.load_complete(
                transaction_id="transaction.phase-d",
                plan_fingerprint="sha256:" + "0" * 64,
                approval_fingerprint=approval.fingerprint,
            )

        now[0] = 700  # Returning a completed exact retry is not new execution.

        def unexpected_execute(
            _self: QualifiedAVMediaAdapter,
            **_kwargs: object,
        ) -> AVMediaExecutionResult:
            raise AssertionError("completed exact retry must not invoke media")

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", unexpected_execute)
        retried = execute_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.phase-d",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: now[0],
        )

        assert retried == receipt
        assert execute_calls == 1
        with pytest.raises(AVReconstructionPipelineError, match="input_identity_mismatch"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.phase-d",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, b"different-private-bytes"),),
                clock_ms=lambda: now[0],
            )
        object.__setattr__(plan, "selective_plan_fingerprint", _fp("post-commit-drift"))
        with pytest.raises(AVReconstructionPipelineError, match="approval_identity_mismatch"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.phase-d",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: now[0],
            )


def test_pipeline_failure_aborts_staging_and_same_identity_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    plan, approval = _executable_plan(source_payload, missing_audio=False)
    now = [400]

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "private-av-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: now[0])

        def fail_after_allocation(
            _self: QualifiedAVMediaAdapter,
            **_kwargs: object,
        ) -> AVMediaExecutionResult:
            raise AVMediaAdapterError("timed_out")

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", fail_after_allocation)
        with pytest.raises(AVMediaAdapterError, match="timed_out"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.retry",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: now[0],
            )

        assert tuple((store_root / "staging").iterdir()) == ()
        assert tuple((store_root / "receipts").iterdir()) == ()
        assert tuple((store_root / "members").iterdir()) == ()

        now[0] = 500

        def succeed(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", succeed)
        receipt = execute_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.retry",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: now[0],
        )
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE


def test_pipeline_rejects_unbound_execution_facts_without_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    plan, approval = _executable_plan(source_payload, missing_audio=False)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "private-av-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def unbound_execution(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            valid = _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )
            return replace(
                valid,
                aggregate_descriptor=replace(
                    valid.aggregate_descriptor,
                    artifact_receipt_fingerprint=_fp("unrelated-plan"),
                ),
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", unbound_execution)
        with pytest.raises(AVReconstructionPipelineError, match="execution_identity_mismatch"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.unbound",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: 400,
            )

        assert tuple((store_root / "staging").iterdir()) == ()
        assert tuple((store_root / "receipts").iterdir()) == ()


@pytest.mark.parametrize("malformed", (("segment.1",), ("segment.1", b"bytes", "extra")))
def test_pipeline_rejects_malformed_payload_pairs_without_store_mutation(
    monkeypatch: pytest.MonkeyPatch,
    malformed: tuple[object, ...],
) -> None:
    source_payload = b"synthetic-private-segment"
    plan, approval = _executable_plan(source_payload, missing_audio=False)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "private-av-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)
        with pytest.raises(AVReconstructionPipelineError, match="input_identity_mismatch"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.malformed",
                plan=plan,
                approval=approval,
                input_payloads=(malformed,),  # type: ignore[arg-type]
                clock_ms=lambda: 400,
            )
        assert tuple((store_root / "staging").iterdir()) == ()


@pytest.mark.parametrize("cancel_after_media", (False, True))
def test_pipeline_cancellation_before_begin_or_publish_leaves_no_transaction(
    monkeypatch: pytest.MonkeyPatch,
    cancel_after_media: bool,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    plan, approval = _executable_plan(source_payload, missing_audio=False)
    cancellation = _Cancellation(cancelled=not cancel_after_media)
    execute_calls = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "private-av-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def execute_then_cancel(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            cancellation.cancelled = True
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", execute_then_cancel)
        with pytest.raises(AVReconstructionPipelineError, match="cancelled"):
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.cancelled",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: 400,
                cancellation=cancellation,
            )

        assert execute_calls == int(cancel_after_media)
        assert tuple((store_root / "staging").iterdir()) == ()
        assert tuple((store_root / "receipts").iterdir()) == ()


def test_missing_complete_lookup_and_exact_abort_are_bounded() -> None:
    payload = b"not-published"
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        assert (
            store.load_complete(
                transaction_id="transaction.missing",
                plan_fingerprint=_fp("plan"),
                approval_fingerprint=_fp("approval"),
            )
            is None
        )
        transaction = store.begin(
            transaction_id="transaction.abort",
            plan_fingerprint=_fp("plan"),
            approval_fingerprint=_fp("approval"),
            expires_at_ms=500,
        )
        lease = store.allocate_output(
            transaction,
            handle="avout_0123456789abcdef0123456789abcdef",
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        lease.path.write_bytes(payload)
        store.abort(transaction)
        assert tuple((Path(temporary) / "private-av-store" / "staging").iterdir()) == ()


def test_abort_cleanup_failure_disables_until_verified_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        transaction = store.begin(
            transaction_id="transaction.abort-failure",
            plan_fingerprint=_fp("plan"),
            approval_fingerprint=_fp("approval"),
            expires_at_ms=500,
        )
        original_cleanup = store._cleanup_transaction

        def fail_cleanup(_transaction: object) -> None:
            raise ArtifactStoreError("injected-private-cleanup-failure")

        monkeypatch.setattr(store, "_cleanup_transaction", fail_cleanup)
        with pytest.raises(AVStoreError, match="cleanup_failed"):
            store.abort(transaction)
        with pytest.raises(AVStoreError, match="store_disabled"):
            store.begin(
                transaction_id="transaction.disabled",
                plan_fingerprint=_fp("plan"),
                approval_fingerprint=_fp("approval"),
                expires_at_ms=500,
            )

        monkeypatch.setattr(store, "_cleanup_transaction", original_cleanup)
        now[0] = 600
        report = store.recover()
        assert report.staging_removed == 1
        assert store.begin(
            transaction_id="transaction.recovered",
            plan_fingerprint=_fp("plan"),
            approval_fingerprint=_fp("approval"),
            expires_at_ms=900,
        )


def test_concurrent_exact_retry_cannot_abort_the_active_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    plan, approval = _executable_plan(source_payload, missing_audio=False)
    entered = threading.Event()
    release = threading.Event()
    execute_calls = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def blocking_execute(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            entered.set()
            assert release.wait(timeout=5)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", blocking_execute)

        def run() -> AVReconstructionReceipt:
            return execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.concurrent",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: 400,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(run)
            assert entered.wait(timeout=5)
            with pytest.raises(AVStoreError, match="transaction_in_progress"):
                run()
            assert any((root / "private-av-store" / "staging").iterdir())
            release.set()
            receipt = first.result(timeout=10)

        assert execute_calls == 1
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        assert run() == receipt


def test_exact_qualified_pipeline_commits_and_reads_private_output() -> None:
    ffmpeg, ffprobe = _qualified_binary_paths_from_environment()
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    now = [250]
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        media_path = root / "source.mp4"
        generated = subprocess.run(  # noqa: S603
            (
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=512x512:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo:d=1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-frames:v",
                "30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-x264-params",
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-metadata",
                "encoder=",
                "-color_range",
                "tv",
                "-colorspace",
                "bt709",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-video_track_timescale",
                "90000",
                "-movflags",
                "+faststart",
                "-threads",
                "1",
                "-n",
                str(media_path),
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            check=False,
            timeout=30,
        )
        assert generated.returncode == 0
        payload = media_path.read_bytes()
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: now[0],
        )
        kwargs = _plan_kwargs(count=1)
        source_receipt = replace(
            kwargs["artifact_receipts"][0],
            output_fingerprint=_fp(payload),
            byte_length=len(payload),
            receipt_fingerprint=None,
        )
        assert source_receipt.output_fingerprint is not None
        descriptor = adapter.inspect_payload(
            segment_id=source_receipt.segment_id,
            artifact_receipt_fingerprint=source_receipt.fingerprint,
            artifact_output_fingerprint=source_receipt.output_fingerprint,
            payload=payload,
        )
        plan, approval = _plan_from_inspected_descriptor(
            receipt=source_receipt,
            descriptor=descriptor,
        )
        store = PrivateAVReconstructionStore(
            root / "private-av-store",
            policy=_policy(),
            clock_ms=lambda: now[0],
        )

        now[0] = 400
        receipt = execute_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.exact-pipeline",
            plan=plan,
            approval=approval,
            input_payloads=((source_receipt.segment_id, payload),),
            clock_ms=lambda: now[0],
        )
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        output = receipt.outputs[0]
        stored = store.read_output(receipt, output.handle, maximum_bytes=_policy().max_member_bytes)
        assert len(stored) == output.byte_length
        assert _fp(stored) == output.content_fingerprint
        assert tuple((root / "private-av-store" / "staging").iterdir()) == ()

        now[0] = 700
        assert (
            execute_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.exact-pipeline",
                plan=plan,
                approval=approval,
                input_payloads=((source_receipt.segment_id, payload),),
                clock_ms=lambda: now[0],
            )
            == receipt
        )


# ---------------------------------------------------------------------------
# M20-08: incremental source-backed inputs through the pipeline seam
# ---------------------------------------------------------------------------


def test_source_backed_inputs_admit_by_declared_identity_and_commit_identical_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-segment"
    aggregate_payload = b"synthetic-private-aggregate"
    derived_payload = b"synthetic-private-derived"
    plan, approval = _executable_plan(source_payload, missing_audio=True)
    now = [400]

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: now[0])

        def fake_execute(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVMediaExecutionResult:
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            derived = cast(tuple[tuple[str, OwnedOutputLease], ...], kwargs["derived_outputs"])
            aggregate.path.write_bytes(aggregate_payload)
            derived[0][1].path.write_bytes(derived_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=derived_payload,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", fake_execute)
        baseline_store = PrivateAVReconstructionStore(
            root / "bytes-store",
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        baseline = execute_av_reconstruction(
            store=baseline_store,
            adapter=adapter,
            transaction_id="transaction.parity",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: now[0],
        )

        source = _ReplaySource(source_payload)
        source_store = PrivateAVReconstructionStore(
            root / "source-store",
            policy=_policy(),
            clock_ms=lambda: now[0],
        )
        incremental = execute_av_reconstruction(
            store=source_store,
            adapter=adapter,
            transaction_id="transaction.parity",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source),),
            clock_ms=lambda: now[0],
        )

        # Parity oracle: an identity-equivalent source input must commit an identical receipt.
        assert incremental == baseline

        # A source whose declared identity diverges from the plan is refused before any
        # byte moves or any media process runs.
        lying = _ReplaySource(
            source_payload,
            declared=(len(source_payload) + 1, _fp(source_payload)),
        )
        with pytest.raises(AVReconstructionPipelineError, match="input_identity_mismatch"):
            execute_av_reconstruction(
                store=PrivateAVReconstructionStore(
                    root / "mismatch-store",
                    policy=_policy(),
                    clock_ms=lambda: now[0],
                ),
                adapter=adapter,
                transaction_id="transaction.mismatch",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, lying),),
                clock_ms=lambda: now[0],
            )
        assert lying.stream_calls == 0
