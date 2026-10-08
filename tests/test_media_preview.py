"""M17-15 bounded aggregate media-preview integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from test_av_reconstruction import (
    _authorities as _workspace_authorities,
)
from test_av_reconstruction import (
    _media as _av_media,
)
from test_av_reconstruction import (
    _plan_kwargs,
)
from test_av_reconstruction import (
    _receipt as _artifact_receipt,
)
from test_av_reconstruction import (
    _spec as _generation_spec,
)
from test_av_reconstruction_pipeline import (
    _adapter,
    _successful_execution,
)
from test_av_reconstruction_pipeline import (
    _policy as _pipeline_policy,
)
from test_av_reconstruction_store import _begin, _policy, _receipt

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
import comfyui_h3_context.adapters.comfyui_media_preview as route_module
import comfyui_h3_context.adapters.comfyui_production_workspace as production_module
import comfyui_h3_context.adapters.media_preview_authority as preview_authority_module
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    AVMediaExecutionResult,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.av_reconstruction_pipeline import (
    AVReconstructionPipelineError,
    execute_production_av_reconstruction,
)
from comfyui_h3_context.adapters.av_reconstruction_store import (
    AVStoreError,
    PrivateAVReconstructionStore,
)
from comfyui_h3_context.adapters.comfyui_media_preview import (
    MEDIA_PREVIEW_REQUEST_SCHEMA,
    decode_media_preview_request_json,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarProductionSeed
from comfyui_h3_context.adapters.media_subprocess import (
    MediaProcessInvocation,
    OwnedOutputLease,
    create_output_lease,
)
from comfyui_h3_context.core.av_reconstruction import (
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionApproval,
    AVReconstructionPlan,
    AVReconstructionReceipt,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
)
from comfyui_h3_context.core.errors import MediaProcessError
from comfyui_h3_context.core.generation_sequence import (
    FingerprintDomain,
    GenerationSequenceProjection,
    build_generation_sequence_projection,
    create_generation_sequence_state,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt
from comfyui_h3_context.core.segment_workspace import (
    MultiSegmentWorkspace,
    create_workspace,
    derive_segment_manifests,
)
from comfyui_h3_context.core.selective_rerun import (
    ArtifactReuseStatus,
    SelectiveArtifactEvidence,
    SelectiveRerunApproval,
    build_approved_generation_sequence_plan,
    build_selective_rerun_plan,
)
from comfyui_h3_context.core.ui_projection import ExecutionCorrelation


def _request() -> dict[str, object]:
    return {
        "schema": MEDIA_PREVIEW_REQUEST_SCHEMA,
        "workspace_handle": "pw_" + "a" * 40,
        "expected_workspace_revision": 7,
        "expected_workspace_fingerprint": "sha256:" + "b" * 64,
        "output_handle": "out_" + "c" * 40,
    }


def _fp(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _production_plan(
    source_payload: bytes,
) -> tuple[
    AVReconstructionPlan,
    AVReconstructionApproval,
    GenerationSequenceProjection,
    tuple[SegmentArtifactReceipt, ...],
]:
    kwargs = _plan_kwargs(count=1)
    artifact = replace(
        kwargs["artifact_receipts"][0],
        output_fingerprint=_fp(source_payload),
        byte_length=len(source_payload),
        receipt_fingerprint=None,
    )
    descriptor = replace(
        kwargs["media_descriptors"][0],
        artifact_receipt_fingerprint=artifact.fingerprint,
        artifact_output_fingerprint=artifact.output_fingerprint,
        artifact_byte_length=artifact.byte_length,
    )
    decision = replace(
        kwargs["selective_plan"].decisions[0],
        reused_receipt_fingerprint=artifact.fingerprint,
    )
    selective = replace(
        kwargs["selective_plan"],
        decisions=(decision,),
        reusable_receipts=(artifact,),
        plan_fingerprint=None,
    )
    selective_approval = replace(
        kwargs["selective_approval"],
        plan_fingerprint=selective.fingerprint,
        approval_fingerprint=None,
    )
    plan = build_av_reconstruction_plan(
        **{
            **kwargs,
            "selective_plan": selective,
            "selective_approval": selective_approval,
            "artifact_receipts": (artifact,),
            "media_descriptors": (descriptor,),
        }
    )
    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    sequence = build_generation_sequence_projection(
        kwargs["generation_state"],
        ExecutionCorrelation("prompt.preview", "node.preview"),
    )
    return plan, approval, sequence, (artifact,)


def _all_current_production_plan(
    source_payload: bytes,
    *,
    derive_segment: bool = False,
) -> tuple[
    AVReconstructionPlan,
    AVReconstructionApproval,
    GenerationSequenceProjection,
    tuple[SegmentArtifactReceipt, ...],
]:
    source = cast(
        MultiSegmentWorkspace,
        _plan_kwargs(count=1)["generation_state"].plan.source_workspace_authority,
    )
    assert source is not None
    previous = create_workspace(
        source.workspace_id,
        source.segments,
        accepted_intent_authorities=_workspace_authorities(source.segments),
        selected_segment_ids=source.selected_segment_ids,
    )
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(source)
    spec = _generation_spec(current_manifests[0])
    predecessor = _artifact_receipt(
        previous_manifests[0],
        spec,
        predecessor_output=None,
    )
    evidence = SelectiveArtifactEvidence(
        segment_id=predecessor.segment_id,
        status=ArtifactReuseStatus.REUSABLE,
        inspected_at_ms=200,
        receipt=predecessor,
    )
    selective = build_selective_rerun_plan(
        source,
        previous_manifests,
        current_manifests,
        requested_segment_ids=(source.segments[0].segment_id,),
        artifact_evidence=(evidence,),
        job_specs=(spec,),
    )
    selective_approval = SelectiveRerunApproval(
        selective.fingerprint,
        (source.segments[0].segment_id,),
    )
    sequence_plan = build_approved_generation_sequence_plan(
        selective,
        selective_approval,
        workspace=source,
        manifests=current_manifests,
        job_specs=(spec,),
        artifact_evidence=(),
        approval_inspected_at_ms=250,
    )
    state = create_generation_sequence_state(sequence_plan)
    job = sequence_plan.jobs[0]
    state = record_generation_projection(
        state,
        job.job_id,
        transaction_id="transaction.preview.current",
        graph_fingerprint=job.graph_fingerprint,
        compiled_prompt_fingerprint=job.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        job.job_id,
        queue_prompt_id="queue.preview.current",
    )
    state = record_generation_running(
        state,
        job.job_id,
        host_owner_id="host.preview.current",
    )
    transaction = state.runtime_for(job.job_id).transaction
    assert transaction is not None
    artifact = replace(
        _artifact_receipt(current_manifests[0], spec, predecessor_output=None),
        transaction_fingerprint=transaction.fingerprint,
        output_fingerprint=_fp(source_payload),
        byte_length=len(source_payload),
        receipt_fingerprint=None,
    )
    state = record_generation_success(
        state,
        job.job_id,
        result_fingerprint=_fp(b"result.preview.current"),
        receipt=artifact,
    )
    projection = build_generation_sequence_projection(
        state,
        ExecutionCorrelation("prompt.preview.current", "node.preview.current"),
    )
    media = _av_media(artifact)
    if derive_segment:
        media = replace(media, stream_count=1, audio=None)
    plan = build_av_reconstruction_plan(
        selective_plan=selective,
        selective_approval=selective_approval,
        generation_state=state,
        artifact_receipts=(artifact,),
        media_descriptors=(media,),
        boundary_evidence=(),
        accepted_boundary_receipt_fingerprints=(),
        capability=_plan_kwargs(count=1)["capability"],
        limits=_plan_kwargs(count=1)["limits"],
        target_profile=_plan_kwargs(count=1)["target_profile"],
        planned_at_ms=300,
    )
    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    return plan, approval, projection, (artifact,)


def _segment_preview_receipt(
    plan: AVReconstructionPlan,
    *,
    aggregate_payload: bytes = b"aggregate-preview-member",
    segment_payload: bytes = b"segment-preview-member",
) -> AVReconstructionReceipt:
    segment = plan.segments[0]
    segment_duration = segment.output_end.fraction - segment.output_start.fraction
    aggregate_duration = (
        plan.segments[-1].output_end.fraction - plan.segments[0].output_start.fraction
    )
    aggregate_handle = "avout_" + "1" * 64
    segment_handle = "avout_" + "2" * 64
    return AVReconstructionReceipt(
        transaction_id="transaction.preview.segment.builder",
        plan_fingerprint=plan.fingerprint,
        approval_fingerprint=_fp(b"approval.preview.segment.builder"),
        selective_plan_fingerprint=plan.selective_plan_fingerprint,
        generation_state_fingerprint=plan.generation_state_fingerprint,
        artifact_receipt_fingerprints=plan.artifact_receipt_fingerprints,
        boundary_receipt_fingerprints=plan.boundary_receipt_fingerprints,
        capability_fingerprint=plan.capability.fingerprint,
        execution_fingerprint=_fp(b"execution.preview.segment.builder"),
        segment_results=(
            AVReceiptSegmentResult(
                segment_id=segment.segment_id,
                artifact_receipt_fingerprint=segment.artifact_receipt_fingerprint,
                operations=segment.operations,
                accounting=replace(segment.accounting),
                output_start=segment.output_start,
                output_end=segment.output_end,
                derived_output_handle=segment_handle,
            ),
        ),
        outputs=(
            AVReceiptOutput(
                handle=aggregate_handle,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=len(aggregate_payload),
                content_fingerprint=_fp(aggregate_payload),
                video_frame_count=sum(item.accounting.emitted_frames for item in plan.segments),
                audio_sample_count=sum(item.accounting.emitted_samples for item in plan.segments),
                duration=AVRational(aggregate_duration.numerator, aggregate_duration.denominator),
            ),
            AVReceiptOutput(
                handle=segment_handle,
                kind=AVOutputKind.SEGMENT_EXPORT,
                byte_length=len(segment_payload),
                content_fingerprint=_fp(segment_payload),
                video_frame_count=segment.accounting.emitted_frames,
                audio_sample_count=segment.accounting.emitted_samples,
                duration=AVRational(segment_duration.numerator, segment_duration.denominator),
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=400,
    )


@pytest.fixture
def production_registry_slot() -> Iterator[Callable[[object], None]]:
    """Install a substitute process production authority for one test, and put it back after.

    M23-47 moved construction into the composition root, so a substitute is installed there rather
    than assigned over a module global. The teardown restores the previous instance, or empties the
    slot when nothing had been built yet, so a test that installs a registry cannot leave it behind
    for the next one.
    """

    previous = composition_root.installed(composition_root.PRODUCTION_WORKSPACE)

    def install(registry: object) -> None:
        composition_root.install(composition_root.PRODUCTION_WORKSPACE, registry)

    yield install
    if previous is None:
        composition_root.reset(composition_root.PRODUCTION_WORKSPACE)
    else:
        composition_root.install(composition_root.PRODUCTION_WORKSPACE, previous)


def test_segment_preview_builder_joins_exact_member_and_closes_eligibility_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = getattr(
        preview_authority_module,
        "build_segment_media_preview_source_authority",
        None,
    )
    assert callable(builder), "M24-02 segment preview authority builder is absent"
    plan, _approval, _sequence, _artifacts = _all_current_production_plan(
        b"segment-builder-source",
        derive_segment=True,
    )
    segment_id = plan.segments[0].segment_id
    opaque_handle = "out_" + "d" * 40

    with tempfile.TemporaryDirectory() as temporary:
        store = PrivateAVReconstructionStore(
            Path(temporary) / "store",
            policy=_pipeline_policy(),
            clock_ms=lambda: 400,
        )
        adapter = object.__new__(QualifiedAVMediaAdapter)
        receipt = _segment_preview_receipt(plan)
        member = receipt.outputs[1]
        verified_handles: list[str] = []

        def verify_selected(
            _receipt: object,
            output_handle: str,
            **_kwargs: object,
        ) -> SimpleNamespace:
            verified_handles.append(output_handle)
            selected = next(item for item in receipt.outputs if item.handle == output_handle)
            return SimpleNamespace(
                byte_length=selected.byte_length,
                content_fingerprint=selected.content_fingerprint,
            )

        monkeypatch.setattr(store, "verify_selected_output", verify_selected)
        source = builder(
            plan=plan,
            receipt=receipt,
            store=store,
            adapter=adapter,
            segment_id=segment_id,
            opaque_output_handle=opaque_handle,
            deadline=1.0,
            clock=lambda: 0.0,
        )
        assert source.opaque_output_handle == opaque_handle
        assert source._native_output_handle == member.handle
        assert source.byte_length == member.byte_length
        assert source.duration_ms == int(member.duration.fraction * 1000)
        assert verified_handles == [member.handle]

        wrong_kind = _segment_preview_receipt(plan)
        object.__setattr__(wrong_kind.outputs[1], "kind", AVOutputKind.RECONSTRUCTION_FULL)
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=wrong_kind,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=receipt,
                store=store,
                adapter=adapter,
                segment_id="segment.foreign",
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        missing_export = _segment_preview_receipt(plan)
        object.__setattr__(missing_export.segment_results[0], "derived_output_handle", None)
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=missing_export,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        accounting_drift = _segment_preview_receipt(plan)
        object.__setattr__(
            accounting_drift.segment_results[0].accounting,
            "emitted_frames",
            accounting_drift.segment_results[0].accounting.emitted_frames + 1,
        )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=accounting_drift,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )
        aggregate_source = preview_authority_module.build_media_preview_source_authority(
            plan=plan,
            receipt=accounting_drift,
            store=store,
            adapter=adapter,
            opaque_output_handle="out_" + "e" * 40,
            deadline=1.0,
            clock=lambda: 0.0,
        )
        assert aggregate_source._native_output_handle == accounting_drift.outputs[0].handle

        span_drift = _segment_preview_receipt(plan)
        drifted_start = span_drift.segment_results[0].output_start.fraction + Fraction(1, 1000)
        object.__setattr__(
            span_drift.segment_results[0],
            "output_start",
            AVRational(drifted_start.numerator, drifted_start.denominator),
        )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=span_drift,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        output_drift = _segment_preview_receipt(plan)
        object.__setattr__(
            output_drift.outputs[1],
            "audio_sample_count",
            output_drift.outputs[1].audio_sample_count + 1,
        )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=output_drift,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        frame_drift = _segment_preview_receipt(plan)
        object.__setattr__(
            frame_drift.outputs[1],
            "video_frame_count",
            frame_drift.outputs[1].video_frame_count + 1,
        )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=frame_drift,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        duration_drift = _segment_preview_receipt(plan)
        drifted_duration = duration_drift.outputs[1].duration.fraction + Fraction(1, 1000)
        object.__setattr__(
            duration_drift.outputs[1],
            "duration",
            AVRational(drifted_duration.numerator, drifted_duration.denominator),
        )
        with pytest.raises(ValueError, match="media_preview_source_ineligible"):
            builder(
                plan=plan,
                receipt=duration_drift,
                store=store,
                adapter=adapter,
                segment_id=segment_id,
                opaque_output_handle=opaque_handle,
                deadline=1.0,
                clock=lambda: 0.0,
            )

        with monkeypatch.context() as bounded:
            bounded.setattr(preview_authority_module, "MAX_MEDIA_PREVIEW_SOURCE_BYTES", 1)
            with pytest.raises(ValueError, match="media_preview_source_ineligible"):
                builder(
                    plan=plan,
                    receipt=receipt,
                    store=store,
                    adapter=adapter,
                    segment_id=segment_id,
                    opaque_output_handle=opaque_handle,
                    deadline=1.0,
                    clock=lambda: 0.0,
                )
        with monkeypatch.context() as bounded:
            bounded.setattr(preview_authority_module, "MAX_MEDIA_PREVIEW_DURATION_MS", 1)
            with pytest.raises(ValueError, match="media_preview_source_ineligible"):
                builder(
                    plan=plan,
                    receipt=receipt,
                    store=store,
                    adapter=adapter,
                    segment_id=segment_id,
                    opaque_output_handle=opaque_handle,
                    deadline=1.0,
                    clock=lambda: 0.0,
                )

        with monkeypatch.context() as stale:
            stale.setattr(
                store,
                "verify_selected_output",
                lambda *_args, **_kwargs: SimpleNamespace(
                    byte_length=member.byte_length,
                    content_fingerprint=_fp(b"foreign-selected-member"),
                ),
            )
            with pytest.raises(ValueError, match="media_preview_source_ineligible"):
                builder(
                    plan=plan,
                    receipt=receipt,
                    store=store,
                    adapter=adapter,
                    segment_id=segment_id,
                    opaque_output_handle=opaque_handle,
                    deadline=1.0,
                    clock=lambda: 0.0,
                )

        with monkeypatch.context() as unavailable:
            unavailable.setattr(
                store,
                "verify_selected_output",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AVStoreError("preview_source_unavailable")
                ),
            )
            with pytest.raises(AVStoreError, match="preview_source_unavailable"):
                builder(
                    plan=plan,
                    receipt=receipt,
                    store=store,
                    adapter=adapter,
                    segment_id=segment_id,
                    opaque_output_handle=opaque_handle,
                    deadline=1.0,
                    clock=lambda: 0.0,
                )


def test_media_preview_request_is_one_closed_exact_identity_object() -> None:
    request = _request()
    assert (
        decode_media_preview_request_json(
            json.dumps(request, separators=(",", ":")).encode("utf-8")
        )
        == request
    )

    for invalid in (
        {**request, "codec": "h264"},
        {**request, "schema": "h3.context.production.media_preview.request.v0"},
        {**request, "workspace_handle": "workspace-private"},
        {**request, "expected_workspace_revision": True},
        {**request, "output_handle": "avout_private-native-handle"},
    ):
        with pytest.raises(ValueError):
            decode_media_preview_request_json(
                json.dumps(invalid, separators=(",", ":")).encode("utf-8")
            )

    duplicate = (
        b'{"schema":"h3.context.production.media_preview.request.v1",'
        b'"workspace_handle":"pw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"expected_workspace_revision":7,"expected_workspace_revision":8,'
        b'"expected_workspace_fingerprint":"sha256:'
        + b"b" * 64
        + b'","output_handle":"out_cccccccccccccccccccccccccccccccccccccccc"}'
    )
    with pytest.raises(ValueError):
        decode_media_preview_request_json(duplicate)


def test_selected_member_verify_and_copy_do_not_use_whole_receipt_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"bounded-private-aggregate"
    receipt = _receipt(payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        transaction = _begin(store)
        source = store.allocate_output(
            transaction,
            handle=receipt.outputs[0].handle,
            kind=AVOutputKind.RECONSTRUCTION_FULL,
        )
        source.path.write_bytes(payload)
        store.commit(
            transaction,
            receipt,
            output_leases=((receipt.outputs[0].handle, source),),
        )
        monkeypatch.setattr(
            store,
            "inspect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("whole scan")),
        )

        verified = store.verify_selected_output(
            receipt,
            receipt.outputs[0].handle,
            maximum_bytes=len(payload),
            deadline=11.0,
            clock=lambda: 10.0,
        )
        assert verified.byte_length == len(payload)

        destination = create_output_lease(root / "scratch", suffix=".mp4")
        copied = store.copy_selected_output(
            receipt,
            receipt.outputs[0].handle,
            destination,
            maximum_bytes=len(payload),
            deadline=11.0,
            clock=lambda: 10.0,
        )
        assert copied == verified
        assert destination.path.read_bytes() == payload
        destination.release()

        with pytest.raises(AVStoreError, match="preview_deadline"):
            store.verify_selected_output(
                receipt,
                receipt.outputs[0].handle,
                maximum_bytes=len(payload),
                deadline=10.0,
                clock=lambda: 10.0,
            )


def test_preview_adapter_runs_closed_three_phase_profile_and_cleans_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-source"
    proxy_payload = b"synthetic-proxy"
    receipt = _receipt(source_payload)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 100)
        probe_calls: list[Path] = []
        invocation_values: list[MediaProcessInvocation] = []

        def copy_selected(
            _self: PrivateAVReconstructionStore,
            _receipt_value: object,
            _handle: str,
            destination: OwnedOutputLease,
            **_kwargs: object,
        ) -> object:
            destination.path.write_bytes(source_payload)
            return SimpleNamespace(byte_length=len(source_payload))

        def probe(
            _self: QualifiedAVMediaAdapter,
            path: Path,
            **_kwargs: object,
        ) -> dict[str, object]:
            probe_calls.append(path)
            source = len(probe_calls) == 1
            return {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "1",
                },
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 512 if source else 320,
                        "height": 512 if source else 320,
                        "pix_fmt": "yuv420p",
                        "avg_frame_rate": "30/1" if source else "15/1",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2 if source else 1,
                        "channel_layout": "stereo" if source else "mono",
                    },
                ],
            }

        def transcode(
            _self: QualifiedAVMediaAdapter,
            invocation: MediaProcessInvocation,
            _cancellation: object,
        ) -> None:
            invocation_values.append(invocation)
            invocation.output_leases[0].path.write_bytes(proxy_payload)

        monkeypatch.setattr(PrivateAVReconstructionStore, "copy_selected_output", copy_selected)
        monkeypatch.setattr(QualifiedAVMediaAdapter, "_preview_probe", probe)
        monkeypatch.setattr(QualifiedAVMediaAdapter, "_run_ffmpeg", transcode)

        body = adapter.execute_preview(
            store=store,
            receipt=receipt,
            output_handle=receipt.outputs[0].handle,
            deadline=time.monotonic() + 10.0,
        )

        assert body == proxy_payload
        assert len(probe_calls) == 2
        assert len(invocation_values) == 1
        invocation = invocation_values[0]
        assert invocation.max_owned_output_bytes == 8 * 1024 * 1024
        assert "-format_whitelist" in invocation.argv
        assert "-codec_whitelist" in invocation.argv
        assert invocation.protocol_whitelist == ("file",)
        assert invocation.argv[invocation.argv.index("-c:v") + 1] == "libx264"
        assert invocation.argv[invocation.argv.index("-c:a") + 1] == "aac"
        assert invocation.argv[invocation.argv.index("-movflags") + 1] == "+faststart"
        assert "copy" not in invocation.argv
        assert list((root / "scratch").iterdir()) == []


@pytest.mark.parametrize("expired", (False, True))
def test_preview_adapter_rejects_before_allocating_scratch(
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 100)
        receipt = _receipt(b"synthetic-private-source")
        cancellation = SimpleNamespace(is_cancelled=lambda: not expired)
        monkeypatch.setattr(
            media_module,
            "create_output_lease",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("scratch must not be allocated")
            ),
        )

        with pytest.raises(
            AVMediaAdapterError,
            match="preview_deadline" if expired else "cancelled",
        ):
            adapter.execute_preview(
                store=store,
                receipt=receipt,
                output_handle=receipt.outputs[0].handle,
                deadline=time.monotonic() - 1.0 if expired else time.monotonic() + 10.0,
                cancellation=cancellation,
            )


def test_preview_adapter_releases_first_lease_when_second_allocation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_policy(),
            clock_ms=lambda: 100,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 100)
        receipt = _receipt(b"synthetic-private-source")
        allocations = 0
        releases: list[OwnedOutputLease] = []
        original_release = OwnedOutputLease.release

        def release(lease: OwnedOutputLease) -> None:
            releases.append(lease)
            original_release(lease)

        def allocate(root_path: str | Path, *, suffix: str) -> OwnedOutputLease:
            nonlocal allocations
            allocations += 1
            if allocations == 2:
                raise MediaProcessError("output lease could not be allocated")
            return create_output_lease(root_path, suffix=suffix)

        monkeypatch.setattr(OwnedOutputLease, "release", release)
        monkeypatch.setattr(media_module, "create_output_lease", allocate)
        with pytest.raises(MediaProcessError, match="could not be allocated"):
            adapter.execute_preview(
                store=store,
                receipt=receipt,
                output_handle=receipt.outputs[0].handle,
                deadline=time.monotonic() + 10.0,
            )

        assert allocations == 2
        assert len(releases) == 1
        assert releases[0].released
        assert list((root / "scratch").iterdir()) == []


def test_preview_probe_rejects_non_mp4_and_non_stereo_source() -> None:
    value: dict[str, object] = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "1",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 512,
                "height": 512,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
            },
        ],
    }
    assert QualifiedAVMediaAdapter._validate_preview_probe(value, source=True) == 1000
    wrong_container = json.loads(json.dumps(value))
    wrong_container["format"]["format_name"] = "matroska,webm"
    with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
        QualifiedAVMediaAdapter._validate_preview_probe(wrong_container, source=True)
    wrong_layout = json.loads(json.dumps(value))
    wrong_layout["streams"][1]["channel_layout"] = "unknown"
    with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
        QualifiedAVMediaAdapter._validate_preview_probe(wrong_layout, source=True)


@pytest.mark.parametrize("source", (True, False))
def test_aggregate_preview_accepts_only_qualified_audio_rate_sentinel(source: bool) -> None:
    value: dict[str, object] = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 512 if source else 320,
                "height": 512 if source else 320,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1" if source else "15/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2 if source else 1,
                "channel_layout": "stereo" if source else "mono",
                "avg_frame_rate": "0/0",
            },
        ],
    }
    original = json.dumps(value)
    assert QualifiedAVMediaAdapter._validate_preview_probe(value, source=source) == 1000
    assert json.dumps(value) == original
    for invalid in ("15/1", "0/1", "", 0, None):
        changed = json.loads(original)
        changed["streams"][1]["avg_frame_rate"] = invalid
        with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
            QualifiedAVMediaAdapter._validate_preview_probe(changed, source=source)
    for sentinel in (True, False):
        changed = json.loads(original)
        if not sentinel:
            changed["streams"][1].pop("avg_frame_rate")
        changed["streams"][1]["unowned"] = None
        with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
            QualifiedAVMediaAdapter._validate_preview_probe(changed, source=source)
    too_long = json.loads(original)
    too_long["format"]["duration"] = "30.001"
    with pytest.raises(AVMediaAdapterError, match="media_output_invalid"):
        QualifiedAVMediaAdapter._validate_preview_probe(too_long, source=source)


@pytest.mark.parametrize("seconds", (1, 15, 30))
def test_real_aggregate_preview_keeps_playable_audio_extent(tmp_path: Path, seconds: int) -> None:
    from comfyui_h3_context.core.av_reconstruction import AVRational

    ffmpeg_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    ffmpeg, ffprobe = (
        Path(ffmpeg_value).resolve(strict=True),
        Path(ffprobe_value).resolve(strict=True),
    )
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=tmp_path / "scratch",
        clock_ms=lambda: 100,
    )
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:size=512x512:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=701:sample_rate=48000:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-threads",
            "1",
            "-n",
            str(source),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    payload = source.read_bytes()
    original = _receipt(payload)
    segment = original.segment_results[0]
    frames, samples = seconds * 30, seconds * 48_000
    receipt = replace(
        original,
        segment_results=(
            replace(
                segment,
                output_end=AVRational(seconds, 1),
                accounting=replace(
                    segment.accounting,
                    decoded_frames=frames,
                    carried_frames=frames,
                    emitted_frames=frames,
                    decoded_samples=samples,
                    carried_samples=samples,
                    emitted_samples=samples,
                ),
            ),
        ),
        outputs=(
            replace(
                original.outputs[0],
                video_frame_count=frames,
                audio_sample_count=samples,
                duration=AVRational(seconds, 1),
            ),
        ),
        receipt_fingerprint=None,
    )
    store = PrivateAVReconstructionStore(tmp_path / "store", policy=_policy(), clock_ms=lambda: 100)
    transaction = _begin(store)
    output = receipt.outputs[0]
    lease = store.allocate_output(transaction, handle=output.handle, kind=output.kind)
    lease.path.write_bytes(payload)
    store.commit(transaction, receipt, output_leases=((output.handle, lease),))
    try:
        body = adapter.execute_preview(
            store=store,
            receipt=receipt,
            output_handle=output.handle,
            deadline=time.monotonic() + 43.0,
        )
        preview = tmp_path / "preview.mp4"
        preview.write_bytes(body)
        probe = adapter._preview_probe(
            preview,
            maximum_bytes=8 * 1024 * 1024,
            deadline=time.monotonic() + 30.0,
            cancellation=None,
        )
        assert adapter._validate_preview_probe(probe, source=False) == seconds * 1000
        captured = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_streams",
                "-show_frames",
                "-of",
                "json",
                str(preview),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        decoded = json.loads(captured.stdout)
        assert sum(row["media_type"] == "video" for row in decoded["frames"]) == seconds * 15
        audio = next(row for row in decoded["streams"] if row["codec_type"] == "audio")
        playable = sum(
            Fraction(row["duration"]) * Fraction(audio["time_base"]) * 48_000
            for row in decoded["frames"]
            if row["media_type"] == "audio"
        )
        assert playable == samples
        assert store.read_output(receipt, output.handle, maximum_bytes=len(payload)) == payload
    finally:
        assert not any((tmp_path / "scratch").iterdir())


class _CutoffClock:
    def __init__(self) -> None:
        self.values = iter((9.999, 10.0))
        self.value = 10.0

    def __call__(self) -> float:
        self.value = next(self.values, self.value)
        return self.value


@pytest.mark.skipif(os.name != "nt", reason="Real Windows media identity pin")
def test_preview_media_hash_obeys_absolute_cutoff() -> None:
    # Native pinning stays real; the separate body-read deadline is platform-independent.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "preview.mp4"
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        with pytest.raises(AVMediaAdapterError, match="preview_deadline"):
            with media_module._pin_regular_media(
                path,
                2 * 1024 * 1024,
                deadline=10.0,
                clock=_CutoffClock(),
            ):
                pass


def test_preview_media_body_read_obeys_absolute_cutoff() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "preview.mp4"
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        with pytest.raises(AVMediaAdapterError, match="preview_deadline"):
            media_module._read_regular_media_body(
                path,
                2 * 1024 * 1024,
                deadline=10.0,
                clock=_CutoffClock(),
            )


def test_typed_production_wrapper_validates_before_execution_and_publishes_after_cached_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_payload = b"synthetic-private-source"
    aggregate_payload = b"synthetic-private-aggregate"
    plan, approval, sequence, artifacts = _production_plan(source_payload)
    execute_calls = 0

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_pipeline_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def execute(
            _self: QualifiedAVMediaAdapter,
            **values: object,
        ) -> AVMediaExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, values["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", execute)
        with pytest.raises(AVReconstructionPipelineError, match="production_authority_mismatch"):
            execute_production_av_reconstruction(
                store=store,
                adapter=adapter,
                transaction_id="transaction.preview.invalid",
                plan=plan,
                approval=approval,
                input_payloads=((plan.segments[0].segment_id, source_payload),),
                clock_ms=lambda: 400,
                generation_sequence=sequence,
                artifact_receipts=(),
                continuity_receipts=(),
            )
        assert execute_calls == 0

        first = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.valid",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        second = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.valid",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        assert first == second
        assert execute_calls == 1


def test_clip_preview_user_journey_attach_select_and_admit_each_current_output(
    monkeypatch: pytest.MonkeyPatch,
    production_registry_slot: Callable[[object], None],
) -> None:
    source_payload = b"synthetic-current-source"
    aggregate_payload = b"synthetic-current-aggregate"
    segment_payload = b"synthetic-current-segment"
    plan, approval, sequence, artifacts = _all_current_production_plan(
        source_payload,
        derive_segment=True,
    )
    source = sequence.state.plan.source_workspace_authority
    assert source is not None
    segment = source.segments[0]
    seed = SidebarProductionSeed(
        task_mode=segment.task_mode,
        source_id=segment.source_id,
        reference_ids=segment.reference_ids,
        duration=segment.duration,
        accepted_intent_fingerprint=segment.accepted_intent_fingerprint,
        profile_fingerprint=segment.profile_fingerprint,
        reference_registry_fingerprint=segment.reference_registry_fingerprint,
        native_binding_fingerprint=segment.native_binding_fingerprint,
        producer_settings_fingerprint=segment.producer_settings_fingerprint,
        seed_fingerprint=_fp(b"seed.preview.current"),
    )
    seed_handle = "ws_" + "s" * 40

    def claim(handle: str) -> SidebarProductionSeed:
        if handle != seed_handle:
            raise KeyError(handle)
        return seed

    registry = ProductionWorkspaceRegistry(seed_claim=claim)
    monkeypatch.setattr(route_module, "ensure_media_preview_route_registered", lambda: True)
    production_registry_slot(registry)
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    created = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.preview.create",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": seed_handle},
        }
    ).projection
    assert isinstance(created, ProductionWorkbenchProjection)
    assert created.workspace_id == plan.workspace_id
    selected_before_completion = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.preview.select.before-completion",
            "action": "set_selection",
            "payload": {
                "workspace_handle": created.workspace_handle,
                "expected_workspace_revision": created.workspace_revision,
                "expected_workspace_fingerprint": created.workspace_fingerprint,
                "segment_ids": [],
            },
        }
    ).projection
    assert isinstance(selected_before_completion, ProductionWorkbenchProjection)
    assert selected_before_completion.workspace_revision == created.workspace_revision + 1
    assert selected_before_completion.generation_sequence is not None
    assert (
        selected_before_completion.generation_sequence.workspace_revision
        == created.workspace_revision
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_pipeline_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)
        execute_calls = 0

        def execute(
            _self: QualifiedAVMediaAdapter,
            **values: object,
        ) -> AVMediaExecutionResult:
            nonlocal execute_calls
            execute_calls += 1
            aggregate = cast(OwnedOutputLease, values["aggregate_output"])
            derived = cast(tuple[tuple[str, OwnedOutputLease], ...], values["derived_outputs"])
            aggregate.path.write_bytes(aggregate_payload)
            assert len(derived) == 1
            derived[0][1].path.write_bytes(segment_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=segment_payload,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", execute)
        receipt = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.current.aggregate",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        cached = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.current.aggregate",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        assert cached == receipt
        assert execute_calls == 1
        published_projection = registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.preview.read.published",
                "action": "read_projection",
                "payload": {"workspace_handle": created.workspace_handle},
            }
        ).projection
        assert isinstance(published_projection, ProductionWorkbenchProjection)
        assert (
            published_projection.workspace_revision == selected_before_completion.workspace_revision
        )
        assert published_projection.reconstruction_state == "complete"
        assert "preview_output" in published_projection.allowed_actions
        assert len(published_projection.outputs) == 2
        assert published_projection.outputs[0].segment_id is None
        assert published_projection.outputs[0].preview is True
        assert published_projection.outputs[1].segment_id == plan.segments[0].segment_id
        assert published_projection.outputs[1].preview is True
        published_entry = registry._entries[published_projection.workspace_handle]
        assert tuple(published_entry.preview_sources) == (
            published_projection.outputs[0].output_handle,
            published_projection.outputs[1].output_handle,
        )
        aggregate_source = published_entry.preview_sources[
            published_projection.outputs[0].output_handle
        ]
        segment_source = published_entry.preview_sources[
            published_projection.outputs[1].output_handle
        ]
        assert segment_source._native_output_handle == receipt.outputs[1].handle
        preview_sources = published_entry.preview_sources
        with pytest.raises(TypeError):
            cast(dict[str, object], preview_sources)[aggregate_source.opaque_output_handle] = (
                segment_source
            )

        publication = registry.publish_reconstruction_authority(
            plan=plan,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
            reconstruction_receipt=receipt,
            store=store,
            adapter=adapter,
            deadline=time.monotonic() + 60.0,
        )
        assert publication.disposition == "published"
        assert publication.projection is not None
        assert tuple(
            (row.output_handle, row.segment_id, row.disposition)
            for row in publication.output_journal
        ) == (
            (published_projection.outputs[0].output_handle, None, "published"),
            (
                published_projection.outputs[1].output_handle,
                plan.segments[0].segment_id,
                "published",
            ),
        )
        with pytest.raises(AttributeError):
            publication.output_journal[0].disposition = (  # type: ignore[misc]
                "attached_without_preview"
            )

        with monkeypatch.context() as isolated:
            isolated.setattr(
                production_module,
                "build_segment_media_preview_source_authority",
                lambda **_kwargs: (_ for _ in ()).throw(
                    ValueError("media_preview_source_ineligible")
                ),
            )
            isolated_publication = registry.publish_reconstruction_authority(
                plan=plan,
                generation_sequence=sequence,
                artifact_receipts=artifacts,
                continuity_receipts=(),
                reconstruction_receipt=receipt,
                store=store,
                adapter=adapter,
                deadline=time.monotonic() + 60.0,
            )
            assert isolated_publication.disposition == "published"
            assert isolated_publication.projection is not None
            assert tuple(output.preview for output in isolated_publication.projection.outputs) == (
                True,
                False,
            )
            assert tuple(row.disposition for row in isolated_publication.output_journal) == (
                "published",
                "attached_without_preview",
            )

        with monkeypatch.context() as bounded:
            bounded.setattr(
                production_module,
                "MAX_MEDIA_PREVIEW_AUTHORITY_SET_BYTES",
                aggregate_source.wire_bytes() + segment_source.wire_bytes() - 1,
            )
            limited_publication = registry.publish_reconstruction_authority(
                plan=plan,
                generation_sequence=sequence,
                artifact_receipts=artifacts,
                continuity_receipts=(),
                reconstruction_receipt=receipt,
                store=store,
                adapter=adapter,
                deadline=time.monotonic() + 60.0,
            )
            assert limited_publication.projection is not None
            assert tuple(output.preview for output in limited_publication.projection.outputs) == (
                True,
                False,
            )
            assert tuple(row.disposition for row in limited_publication.output_journal) == (
                "published",
                "attached_without_preview",
            )

        restored_publication = registry.publish_reconstruction_authority(
            plan=plan,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
            reconstruction_receipt=receipt,
            store=store,
            adapter=adapter,
            deadline=time.monotonic() + 60.0,
        )
        assert restored_publication.projection is not None
        published_projection = restored_publication.projection
        assert tuple(output.preview for output in published_projection.outputs) == (True, True)
        published_entry = registry._entries[published_projection.workspace_handle]
        preview_sources = published_entry.preview_sources
        aggregate_source = preview_sources[published_projection.outputs[0].output_handle]
        segment_source = preview_sources[published_projection.outputs[1].output_handle]

        before_capacity_failure = published_entry
        with monkeypatch.context() as bounded:
            bounded.setattr(production_module, "MAX_PRODUCTION_AUTHORITY_BYTES", 1)
            blocked_publication = registry.publish_reconstruction_authority(
                plan=plan,
                generation_sequence=sequence,
                artifact_receipts=artifacts,
                continuity_receipts=(),
                reconstruction_receipt=receipt,
                store=store,
                adapter=adapter,
                deadline=time.monotonic() + 60.0,
            )
            assert blocked_publication.disposition == "workspace_capacity"
            assert blocked_publication.projection is None
            assert blocked_publication.output_journal == ()
            assert (
                registry._entries[published_projection.workspace_handle] is before_capacity_failure
            )

        retained_projection = registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.preview.select.after-completion",
                "action": "set_selection",
                "payload": {
                    "workspace_handle": published_projection.workspace_handle,
                    "expected_workspace_revision": published_projection.workspace_revision,
                    "expected_workspace_fingerprint": published_projection.workspace_fingerprint,
                    "segment_ids": [published_projection.segments[0].segment_id],
                },
            }
        ).projection
        assert isinstance(retained_projection, ProductionWorkbenchProjection)
        assert retained_projection.workspace_revision == published_projection.workspace_revision + 1
        assert retained_projection.run_state == published_projection.run_state
        assert retained_projection.reconstruction_state == "complete"
        assert retained_projection.outputs == published_projection.outputs
        assert retained_projection.selected_segment_ids == (
            published_projection.segments[0].segment_id,
        )
        assert retained_projection.authority_versions == published_projection.authority_versions
        assert "preview_output" in retained_projection.allowed_actions
        retained_entry = registry._entries[retained_projection.workspace_handle]
        assert tuple(retained_entry.preview_sources) == tuple(preview_sources)
        assert retained_entry.authority_bytes == (
            len(retained_entry.workspace.to_wire_bytes())
            + retained_entry.accepted_authorities.wire_bytes()
            + sum(source.wire_bytes() for source in retained_entry.preview_sources.values())
        )
        admitted = registry.admit_media_preview_source(
            workspace_handle=retained_projection.workspace_handle,
            expected_workspace_revision=retained_projection.workspace_revision,
            expected_workspace_fingerprint=retained_projection.workspace_fingerprint,
            output_handle=retained_projection.outputs[0].output_handle,
        )
        admitted_segment = registry.admit_media_preview_source(
            workspace_handle=retained_projection.workspace_handle,
            expected_workspace_revision=retained_projection.workspace_revision,
            expected_workspace_fingerprint=retained_projection.workspace_fingerprint,
            output_handle=retained_projection.outputs[1].output_handle,
        )
        assert admitted is aggregate_source
        assert admitted_segment is segment_source
        assert registry.media_preview_source_is_current(
            workspace_handle=retained_projection.workspace_handle,
            expected_workspace_revision=retained_projection.workspace_revision,
            expected_workspace_fingerprint=retained_projection.workspace_fingerprint,
            source=admitted_segment,
        )
        with pytest.raises(production_module.ProductionWorkbenchError) as unknown:
            registry.admit_media_preview_source(
                workspace_handle=retained_projection.workspace_handle,
                expected_workspace_revision=retained_projection.workspace_revision,
                expected_workspace_fingerprint=retained_projection.workspace_fingerprint,
                output_handle="out_" + "z" * 40,
            )
        assert (unknown.value.code, unknown.value.status) == ("preview_output_unknown", 404)

        structural_registry = ProductionWorkspaceRegistry(
            seed_claim=registry._seed_claim,
            clock=lambda: 0.0,
        )
        structural_registry._entries[retained_projection.workspace_handle] = (
            production_module._ProductionEntry(
                workspace=retained_entry.workspace,
                touched_at=0.0,
                authority_bytes=retained_entry.authority_bytes,
                accepted_authorities=retained_entry.accepted_authorities,
                preview_sources=retained_entry.preview_sources,
            )
        )
        structurally_changed = structural_registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.preview.structural-drop",
                "action": "set_segment_relation",
                "payload": {
                    "workspace_handle": retained_projection.workspace_handle,
                    "expected_workspace_revision": retained_projection.workspace_revision,
                    "expected_workspace_fingerprint": retained_projection.workspace_fingerprint,
                    "segment_id": retained_projection.segments[0].segment_id,
                    "relation": "cut",
                    "predecessor_segment_id": None,
                },
            }
        ).projection
        assert isinstance(structurally_changed, ProductionWorkbenchProjection)
        assert structurally_changed.outputs == ()
        assert "preview_output" not in structurally_changed.allowed_actions
        assert not structural_registry._entries[
            structurally_changed.workspace_handle
        ].preview_sources

        monkeypatch.setattr(route_module, "ensure_media_preview_route_registered", lambda: False)
        unavailable_publication = registry.publish_reconstruction_authority(
            plan=plan,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
            reconstruction_receipt=receipt,
            store=store,
            adapter=adapter,
            deadline=time.monotonic() + 60.0,
        )
        assert unavailable_publication.disposition == "attached_without_preview"
        assert unavailable_publication.projection is not None
        assert tuple(row.disposition for row in unavailable_publication.output_journal) == (
            "attached_without_preview",
            "attached_without_preview",
        )
        unavailable_receipt = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.current.aggregate",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        assert unavailable_receipt == receipt
        unavailable_projection = registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.preview.read.route-unavailable",
                "action": "read_projection",
                "payload": {"workspace_handle": created.workspace_handle},
            }
        ).projection

        monkeypatch.setattr(route_module, "ensure_media_preview_route_registered", lambda: True)
        monkeypatch.setattr(preview_authority_module, "MAX_MEDIA_PREVIEW_SOURCE_BYTES", 1)
        ineligible_receipt = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.current.aggregate",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )
        assert ineligible_receipt == receipt
        ineligible_projection = registry.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": "request.preview.read.limit-ineligible",
                "action": "read_projection",
                "payload": {"workspace_handle": created.workspace_handle},
            }
        ).projection

    assert isinstance(retained_projection, ProductionWorkbenchProjection)
    assert "preview_output" in retained_projection.allowed_actions
    assert len(retained_projection.outputs) == 2
    assert admitted.opaque_output_handle == retained_projection.outputs[0].output_handle
    assert isinstance(unavailable_projection, ProductionWorkbenchProjection)
    assert "preview_output" not in unavailable_projection.allowed_actions
    assert not any(output.preview for output in unavailable_projection.outputs)
    assert isinstance(ineligible_projection, ProductionWorkbenchProjection)
    assert "preview_output" not in ineligible_projection.allowed_actions
    assert not any(output.preview for output in ineligible_projection.outputs)


def test_valid_selective_reuse_returns_receipt_without_mutating_production(
    monkeypatch: pytest.MonkeyPatch,
    production_registry_slot: Callable[[object], None],
) -> None:
    source_payload = b"synthetic-reused-source"
    aggregate_payload = b"synthetic-reused-aggregate"
    plan, approval, sequence, artifacts = _production_plan(source_payload)
    source = sequence.state.plan.source_workspace_authority
    assert source is not None
    segment = source.segments[0]
    seed = SidebarProductionSeed(
        task_mode=segment.task_mode,
        source_id=segment.source_id,
        reference_ids=segment.reference_ids,
        duration=segment.duration,
        accepted_intent_fingerprint=segment.accepted_intent_fingerprint,
        profile_fingerprint=segment.profile_fingerprint,
        reference_registry_fingerprint=segment.reference_registry_fingerprint,
        native_binding_fingerprint=segment.native_binding_fingerprint,
        producer_settings_fingerprint=segment.producer_settings_fingerprint,
        seed_fingerprint=_fp(b"seed.preview.reuse"),
    )
    seed_handle = "ws_" + "r" * 40

    def claim_seed(handle: str) -> SidebarProductionSeed:
        if handle == seed_handle:
            return seed
        raise KeyError(handle)

    registry = ProductionWorkspaceRegistry(seed_claim=claim_seed)
    production_registry_slot(registry)
    monkeypatch.setattr(route_module, "ensure_media_preview_route_registered", lambda: True)
    assert registry.publish_generation_sequence_authority(sequence, seed_handle) is None
    before = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.preview.reuse.create",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": seed_handle},
        }
    ).projection
    assert before is not None

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = PrivateAVReconstructionStore(
            root / "store",
            policy=_pipeline_policy(),
            clock_ms=lambda: 400,
        )
        adapter = _adapter(root, monkeypatch, clock_ms=lambda: 400)

        def execute(
            _self: QualifiedAVMediaAdapter,
            **values: object,
        ) -> AVMediaExecutionResult:
            aggregate = cast(OwnedOutputLease, values["aggregate_output"])
            aggregate.path.write_bytes(aggregate_payload)
            return _successful_execution(
                plan=plan,
                approval=approval,
                segment_id=plan.segments[0].segment_id,
                aggregate_payload=aggregate_payload,
                derived_payload=None,
            )

        monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_plan", execute)
        receipt = execute_production_av_reconstruction(
            store=store,
            adapter=adapter,
            transaction_id="transaction.preview.reuse",
            plan=plan,
            approval=approval,
            input_payloads=((plan.segments[0].segment_id, source_payload),),
            clock_ms=lambda: 400,
            generation_sequence=sequence,
            artifact_receipts=artifacts,
            continuity_receipts=(),
        )

    after = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "request.preview.reuse.read",
            "action": "read_projection",
            "payload": {"workspace_handle": before.workspace_handle},
        }
    ).projection
    assert receipt.publication_state.value == "complete"
    assert isinstance(after, ProductionWorkbenchProjection)
    assert after.to_wire() == before.to_wire()
    assert "preview_output" not in after.allowed_actions
