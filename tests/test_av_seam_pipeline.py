"""M20-09 focused tests: seamed pipeline integration and real-binary rows."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_av_reconstruction import _plan_kwargs
from test_av_reconstruction_media import _qualified_binary_paths_from_environment
from test_av_reconstruction_pipeline import (
    _adapter,
    _Cancellation,
    _fp,
    _output_descriptor,
    _policy,
)
from test_av_seam_policy import _blend_spec

from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    AVSeamExecutionResult,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.av_reconstruction_pipeline import (
    AVReconstructionPipelineError,
    execute_av_reconstruction,
    execute_seamed_av_reconstruction,
)
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AVStoreInspectionStatus,
    AVStoreReceiptCodec,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.adapters.media_subprocess import OwnedOutputLease
from comfyui_h3_context.core.av_reconstruction import (
    AVRational,
    AVReconstructionApproval,
    AVReconstructionPlan,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.av_seam_policy import (
    AV_SEAM_RECEIPT_SCHEMA,
    MAX_AV_SEAM_RECEIPT_BYTES,
    AVSeamApproval,
    AVSeamAudioPolicy,
    AVSeamOperation,
    AVSeamPlan,
    AVSeamReconstructionReceipt,
    AVSeamSpec,
    approve_av_seam_plan,
    build_av_seam_plan,
    decode_av_seam_reconstruction_receipt,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt


def _seam_codec() -> AVStoreReceiptCodec:
    return AVStoreReceiptCodec(
        schema_id=AV_SEAM_RECEIPT_SCHEMA,
        receipt_type=AVSeamReconstructionReceipt,
        decode=decode_av_seam_reconstruction_receipt,
        max_receipt_bytes=MAX_AV_SEAM_RECEIPT_BYTES,
    )


def _payload_bound_plan(
    payloads: tuple[bytes, ...],
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    """A count=len(payloads) plan whose receipts fingerprint the exact bytes."""

    kwargs = _plan_kwargs(count=len(payloads))
    receipts = tuple(
        replace(
            receipt,
            output_fingerprint=_fp(payload),
            byte_length=len(payload),
            receipt_fingerprint=None,
        )
        for receipt, payload in zip(kwargs["artifact_receipts"], payloads, strict=True)
    )
    descriptors = tuple(
        replace(
            descriptor,
            artifact_receipt_fingerprint=receipt.fingerprint,
            artifact_output_fingerprint=receipt.output_fingerprint,
            artifact_byte_length=receipt.byte_length,
        )
        for descriptor, receipt in zip(kwargs["media_descriptors"], receipts, strict=True)
    )
    return _plan_from_inspected(receipts, descriptors)


def _seamed_fixture() -> tuple[
    AVReconstructionPlan,
    AVReconstructionApproval,
    AVSeamPlan,
    AVSeamApproval,
    tuple[tuple[str, bytes], ...],
]:
    payloads = (b"synthetic-private-segment-1", b"synthetic-private-segment-2")
    plan, approval = _payload_bound_plan(payloads)
    seam_plan = build_av_seam_plan(
        base_plan=plan,
        base_approval=approval,
        seam_specs=(_blend_spec(15),),
        planned_at_ms=320,
    )
    seam_approval = approve_av_seam_plan(
        seam_plan,
        base_approval=approval,
        approved_at_ms=330,
        expires_at_ms=600,
    )
    input_payloads = tuple(
        (segment.segment_id, payload)
        for segment, payload in zip(plan.segments, payloads, strict=True)
    )
    return plan, approval, seam_plan, seam_approval, input_payloads


def _seam_execution(
    *,
    plan: AVReconstructionPlan,
    approval: AVReconstructionApproval,
    seam_plan: AVSeamPlan,
    seam_approval: AVSeamApproval,
    aggregate_payload: bytes,
) -> AVSeamExecutionResult:
    descriptor = _output_descriptor(
        segment_id="seam_reconstruction.aggregate",
        authority_fingerprint=seam_plan.fingerprint,
        payload=aggregate_payload,
    )
    assert descriptor.video is not None
    assert descriptor.audio is not None
    descriptor = replace(
        descriptor,
        video=replace(
            descriptor.video,
            end_time=AVRational(3, 2),
            decoded_frame_count=45,
        ),
        audio=replace(
            descriptor.audio,
            end_time=AVRational(3, 2),
            decoded_sample_count=72_000,
        ),
    )
    execution_fingerprint = canonical_fingerprint(
        {
            "plan_fingerprint": plan.fingerprint,
            "approval_fingerprint": approval.fingerprint,
            "seam_plan_fingerprint": seam_plan.fingerprint,
            "seam_approval_fingerprint": seam_approval.fingerprint,
            "capability_fingerprint": plan.capability.fingerprint,
            "seam_capability_fingerprint": seam_plan.seam_capability_fingerprint,
            "aggregate": descriptor.to_wire(),
        }
    )
    return AVSeamExecutionResult(
        execution_fingerprint=execution_fingerprint,
        aggregate_descriptor=descriptor,
    )


def test_seamed_pipeline_requires_the_seam_store_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, approval, seam_plan, seam_approval, input_payloads = _seamed_fixture()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "v1-store",
            policy=_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)
        with pytest.raises(AVReconstructionPipelineError, match="seam_store_codec"):
            execute_seamed_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.seam",
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                input_payloads=input_payloads,
                clock_ms=lambda: 400,
            )


def test_seamed_pipeline_commits_and_retries_the_exact_seam_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, approval, seam_plan, seam_approval, input_payloads = _seamed_fixture()
    aggregate_payload = b"synthetic-seam-aggregate"
    execute_calls = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: 400,
            receipt_codec=_seam_codec(),
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def fake_execute(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVSeamExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            return _seam_execution(
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                aggregate_payload=aggregate_payload,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_seam_plan", fake_execute)
        receipt = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 400,
        )
        assert execute_calls == 1
        assert receipt.plan_fingerprint == seam_plan.fingerprint
        assert receipt.base_plan_fingerprint == plan.fingerprint
        assert receipt.total_output_frames == 45
        assert receipt.total_output_samples == 72_000
        assert receipt.seams == seam_plan.seams
        assert len(receipt.outputs) == 1
        assert receipt.outputs[0].handle.startswith("avout_")
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE
        assert (
            store.read_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=1024,
            )
            == aggregate_payload
        )

        again = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 400,
        )
        assert execute_calls == 1
        assert again == receipt


def test_seamed_pipeline_mid_aggregate_cancellation_releases_staging_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, approval, seam_plan, seam_approval, input_payloads = _seamed_fixture()
    cancellation = _Cancellation()
    aggregate_path: Path | None = None
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store_root = root / "seam-store"
        store = PrivateAVReconstructionStore(
            store_root,
            policy=_policy(),
            clock_ms=lambda: 400,
            receipt_codec=_seam_codec(),
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def cancel_mid_aggregate(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVSeamExecutionResult:
            nonlocal aggregate_path
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate_path = aggregate.path
            aggregate.path.write_bytes(b"synthetic-partial-seam-aggregate")
            cancellation.cancelled = True
            raise AVMediaAdapterError("cancelled")

        monkeypatch.setattr(
            QualifiedAVMediaAdapter,
            "execute_seam_plan",
            cancel_mid_aggregate,
        )
        with pytest.raises(AVMediaAdapterError, match="cancelled"):
            execute_seamed_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.seam.cancelled",
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                input_payloads=input_payloads,
                clock_ms=lambda: 400,
                cancellation=cancellation,
            )

        assert aggregate_path is not None
        assert not aggregate_path.exists()
        assert tuple((store_root / "staging").iterdir()) == ()
        assert tuple((store_root / "receipts").iterdir()) == ()
        assert (
            store.load_complete(
                transaction_id="transaction.seam.cancelled",
                plan_fingerprint=seam_plan.fingerprint,
                approval_fingerprint=seam_approval.fingerprint,
            )
            is None
        )


def test_seamed_pipeline_rejects_unbound_execution_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, approval, seam_plan, seam_approval, input_payloads = _seamed_fixture()
    aggregate_payload = b"synthetic-seam-aggregate"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: 400,
            receipt_codec=_seam_codec(),
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def drifted_execute(
            _self: QualifiedAVMediaAdapter,
            **kwargs: object,
        ) -> AVSeamExecutionResult:
            aggregate = cast(OwnedOutputLease, kwargs["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            valid = _seam_execution(
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                aggregate_payload=aggregate_payload,
            )
            return replace(valid, execution_fingerprint=_fp("drifted.execution"))

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_seam_plan", drifted_execute)
        with pytest.raises(AVReconstructionPipelineError, match="execution_identity_mismatch"):
            execute_seamed_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.seam",
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=seam_approval,
                input_payloads=input_payloads,
                clock_ms=lambda: 400,
            )
        assert (
            store.load_complete(
                transaction_id="transaction.seam",
                plan_fingerprint=seam_plan.fingerprint,
                approval_fingerprint=seam_approval.fingerprint,
            )
            is None
        )


def test_seamed_pipeline_rebinds_identity_before_any_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, approval, seam_plan, _seam_approval, input_payloads = _seamed_fixture()
    other_seam_plan = build_av_seam_plan(
        base_plan=plan,
        base_approval=approval,
        seam_specs=(AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),),
        planned_at_ms=320,
    )
    other_approval = approve_av_seam_plan(
        other_seam_plan,
        base_approval=approval,
        approved_at_ms=330,
        expires_at_ms=600,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: 400,
            receipt_codec=_seam_codec(),
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)
        with pytest.raises(AVReconstructionPipelineError, match="seam_approval_identity_mismatch"):
            execute_seamed_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.seam",
                plan=plan,
                approval=approval,
                seam_plan=seam_plan,
                seam_approval=other_approval,
                input_payloads=input_payloads,
                clock_ms=lambda: 400,
            )


def _generate_fixture(ffmpeg: Path, media_path: Path, *, color: str, seconds: int) -> bytes:
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=512x512:r=30:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={seconds}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-frames:v",
        str(30 * seconds),
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
    ]
    generated = subprocess.run(  # noqa: S603
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        shell=False,
        check=False,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stderr.decode("utf-8", "replace")
    return media_path.read_bytes()


def _plan_from_inspected(
    receipts: tuple[SegmentArtifactReceipt, ...],
    descriptors: tuple[Any, ...],
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    kwargs = _plan_kwargs(count=len(receipts))
    decisions = tuple(
        replace(decision, reused_receipt_fingerprint=receipt.fingerprint)
        for decision, receipt in zip(kwargs["selective_plan"].decisions, receipts, strict=True)
    )
    selective_plan = replace(
        kwargs["selective_plan"],
        decisions=decisions,
        reusable_receipts=receipts,
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective_plan.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective_plan,
            "selective_approval": selective_approval,
            "artifact_receipts": receipts,
            "media_descriptors": descriptors,
        }
    )
    return plan, approve_av_reconstruction_plan(
        plan,
        approved_at_ms=310,
        expires_at_ms=600,
    )


def test_real_binary_crossfade_executes_with_exact_probed_totals() -> None:
    ffmpeg, ffprobe = _qualified_binary_paths_from_environment()
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        now_ms = [250]
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: now_ms[0],
        )
        kwargs = _plan_kwargs(count=2)
        payloads: list[bytes] = []
        receipts: list[SegmentArtifactReceipt] = []
        descriptors: list[Any] = []
        for index, color in enumerate(("black", "white")):
            payload = _generate_fixture(
                ffmpeg, root / f"segment-{index}.mp4", color=color, seconds=1
            )
            payloads.append(payload)
            receipt = replace(
                kwargs["artifact_receipts"][index],
                output_fingerprint=_fp(payload),
                byte_length=len(payload),
                receipt_fingerprint=None,
            )
            assert receipt.output_fingerprint is not None
            receipts.append(receipt)
            now_ms[0] = 250
            descriptors.append(
                adapter.inspect_payload(
                    segment_id=receipt.segment_id,
                    artifact_receipt_fingerprint=receipt.fingerprint,
                    artifact_output_fingerprint=receipt.output_fingerprint,
                    payload=payload,
                )
            )
        plan, approval = _plan_from_inspected(tuple(receipts), tuple(descriptors))
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15),),
            planned_at_ms=320,
        )
        seam_approval = approve_av_seam_plan(
            seam_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        store_now = [450]
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: store_now[0],
            receipt_codec=_seam_codec(),
        )
        now_ms[0] = 400
        input_payloads = tuple(
            (receipt.segment_id, payload)
            for receipt, payload in zip(receipts, payloads, strict=True)
        )
        receipt = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam.real",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        assert receipt.total_output_frames == 45
        assert receipt.total_output_samples == 72_000
        assert receipt.outputs[0].duration == AVRational(3, 2)
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE

        # Determinism: a fresh transaction over identical inputs re-encodes to
        # the identical aggregate bytes under the pinned binary.
        second = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam.real.again",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        assert second.outputs[0].content_fingerprint == receipt.outputs[0].content_fingerprint
        assert second.execution_fingerprint == receipt.execution_fingerprint

        # AC-M20-09-04: the all-direct seam path and the legacy path produce the
        # identical aggregate content.
        all_direct_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),),
            planned_at_ms=320,
        )
        all_direct_approval = approve_av_seam_plan(
            all_direct_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        direct_receipt = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam.direct",
            plan=plan,
            approval=approval,
            seam_plan=all_direct_plan,
            seam_approval=all_direct_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        legacy_store = PrivateAVReconstructionStore(
            root / "legacy-store",
            policy=_policy(),
            clock_ms=lambda: store_now[0],
        )
        legacy_receipt = execute_av_reconstruction(
            store=legacy_store,
            adapter=adapter,
            transaction_id="transaction.legacy",
            plan=plan,
            approval=approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        legacy_full = next(iter(legacy_receipt.outputs))
        assert direct_receipt.outputs[0].content_fingerprint == (legacy_full.content_fingerprint)
        assert direct_receipt.total_output_frames == 60
        assert direct_receipt.total_output_samples == 96_000


@pytest.mark.parametrize(
    "audio_policy",
    (AVSeamAudioPolicy.PREDECESSOR, AVSeamAudioPolicy.SUCCESSOR),
)
def test_real_binary_hold_crossfades_execute_with_exact_probed_totals(
    audio_policy: AVSeamAudioPolicy,
) -> None:
    ffmpeg, ffprobe = _qualified_binary_paths_from_environment()
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        now_ms = [250]
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: now_ms[0],
        )
        kwargs = _plan_kwargs(count=2)
        payloads: list[bytes] = []
        receipts: list[SegmentArtifactReceipt] = []
        descriptors: list[Any] = []
        for index, color in enumerate(("black", "white")):
            payload = _generate_fixture(
                ffmpeg, root / f"segment-{index}.mp4", color=color, seconds=1
            )
            payloads.append(payload)
            receipt = replace(
                kwargs["artifact_receipts"][index],
                output_fingerprint=_fp(payload),
                byte_length=len(payload),
                receipt_fingerprint=None,
            )
            assert receipt.output_fingerprint is not None
            receipts.append(receipt)
            now_ms[0] = 250
            descriptors.append(
                adapter.inspect_payload(
                    segment_id=receipt.segment_id,
                    artifact_receipt_fingerprint=receipt.fingerprint,
                    artifact_output_fingerprint=receipt.output_fingerprint,
                    payload=payload,
                )
            )
        plan, approval = _plan_from_inspected(tuple(receipts), tuple(descriptors))
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(
                AVSeamSpec(
                    operation=AVSeamOperation.CROSSFADE,
                    overlap_frames=15,
                    audio_policy=audio_policy,
                    predecessor_master_source_id="master.left",
                    successor_master_source_id="master.right",
                ),
            ),
            planned_at_ms=320,
        )
        seam_approval = approve_av_seam_plan(
            seam_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: 450,
            receipt_codec=_seam_codec(),
        )
        now_ms[0] = 400
        input_payloads = tuple(
            (receipt.segment_id, payload)
            for receipt, payload in zip(receipts, payloads, strict=True)
        )
        receipt = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id=f"transaction.seam.hold.{audio_policy.value}",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        assert receipt.total_output_frames == 45
        assert receipt.total_output_samples == 72_000
        assert receipt.outputs[0].duration == AVRational(3, 2)
        assert receipt.seams[0].audio_policy is audio_policy
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE


def test_real_binary_interior_crossfade_holds_exact_totals_away_from_the_tail() -> None:
    """M20-09 review finding 2: the crossfade boundary sits between two direct
    joins, so the blend region is nowhere adjacent to the final trim guard and a
    boundary miscount could not be absorbed by the tail crop without moving the
    probed frame/sample totals published in the receipt."""

    ffmpeg, ffprobe = _qualified_binary_paths_from_environment()
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        now_ms = [250]
        adapter = QualifiedAVMediaAdapter(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(root / "scratch").resolve(),
            clock_ms=lambda: now_ms[0],
        )
        kwargs = _plan_kwargs(count=4)
        payloads: list[bytes] = []
        receipts: list[SegmentArtifactReceipt] = []
        descriptors: list[Any] = []
        for index, color in enumerate(("black", "white", "gray", "blue")):
            payload = _generate_fixture(
                ffmpeg, root / f"segment-{index}.mp4", color=color, seconds=1
            )
            payloads.append(payload)
            receipt = replace(
                kwargs["artifact_receipts"][index],
                output_fingerprint=_fp(payload),
                byte_length=len(payload),
                receipt_fingerprint=None,
            )
            assert receipt.output_fingerprint is not None
            receipts.append(receipt)
            now_ms[0] = 250
            descriptors.append(
                adapter.inspect_payload(
                    segment_id=receipt.segment_id,
                    artifact_receipt_fingerprint=receipt.fingerprint,
                    artifact_output_fingerprint=receipt.output_fingerprint,
                    payload=payload,
                )
            )
        plan, approval = _plan_from_inspected(tuple(receipts), tuple(descriptors))
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(
                AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
                _blend_spec(15),
                AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
            ),
            planned_at_ms=320,
        )
        seam_approval = approve_av_seam_plan(
            seam_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        store = PrivateAVReconstructionStore(
            root / "seam-store",
            policy=_policy(),
            clock_ms=lambda: 450,
            receipt_codec=_seam_codec(),
        )
        now_ms[0] = 400
        input_payloads = tuple(
            (receipt.segment_id, payload)
            for receipt, payload in zip(receipts, payloads, strict=True)
        )
        receipt = execute_seamed_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.seam.interior",
            plan=plan,
            approval=approval,
            seam_plan=seam_plan,
            seam_approval=seam_approval,
            input_payloads=input_payloads,
            clock_ms=lambda: 450,
        )
        assert receipt.total_output_frames == 105
        assert receipt.total_output_samples == 168_000
        assert receipt.outputs[0].duration == AVRational(7, 2)
        assert store.inspect(receipt).status is AVStoreInspectionStatus.COMPLETE


def test_real_binary_declares_every_seam_filter() -> None:
    ffmpeg, _ffprobe = _qualified_binary_paths_from_environment()
    listed = subprocess.run(  # noqa: S603
        [str(ffmpeg), "-hide_banner", "-filters"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        shell=False,
        check=False,
        timeout=30,
    )
    assert listed.returncode == 0
    catalogue = listed.stdout.decode("utf-8", "replace")
    names = {line.split()[1] for line in catalogue.splitlines() if len(line.split()) > 1}
    for required in qualified_ffmpeg_capability().filters:
        assert required in names, required
