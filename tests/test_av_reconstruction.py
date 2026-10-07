"""Focused M17-11 pure-core reconstruction and sole-receipt tests."""

from __future__ import annotations

import hashlib
import json
from copy import copy
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

import comfyui_h3_context.core.av_reconstruction as av_reconstruction_module
from comfyui_h3_context.core.av_reconstruction import (
    AVAudioDescriptor,
    AVBoundaryEvidence,
    AVMediaDescriptor,
    AVOperation,
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReceiptSegmentResult,
    AVReconstructionError,
    AVReconstructionReceipt,
    AVStreamAccounting,
    AVTargetProfile,
    AVVideoDescriptor,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
    decode_av_reconstruction_receipt,
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.continuity_handoff import (
    ContinuityBoundaryReceipt,
    ContinuityMode,
)
from comfyui_h3_context.core.contracts import TaskMode
from comfyui_h3_context.core.generation_sequence import (
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequenceState,
    create_generation_sequence_state,
    record_generation_projection,
    record_generation_running,
    record_generation_submission,
    record_generation_success,
)
from comfyui_h3_context.core.segment_artifacts import (
    ArtifactLifecycleState,
    SegmentArtifactReceipt,
)
from comfyui_h3_context.core.segment_workspace import (
    AcceptedIntentAuthority,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    create_workspace,
    derive_segment_manifests,
    revise_workspace,
)
from comfyui_h3_context.core.selective_rerun import (
    ArtifactReuseStatus,
    SelectiveArtifactEvidence,
    SelectiveRerunApproval,
    SelectiveRerunPlan,
    build_approved_generation_sequence_plan,
    build_selective_rerun_plan,
)


def _fp(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


def _segment(segment_id: str, *, predecessor: str | None = None) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=TaskMode.T2VA,
        source_id=f"source.{segment_id}",
        reference_ids=(),
        duration=SegmentDuration.from_frame_count(39),
        relation=(
            SegmentRelationKind.INDEPENDENT
            if predecessor is None
            else SegmentRelationKind.PREDECESSOR
        ),
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fp(f"intent.{segment_id}"),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=_fp(f"profile.{segment_id}"),
        reference_registry_fingerprint=_fp(f"registry.{segment_id}"),
        native_binding_fingerprint=_fp(f"native.{segment_id}"),
        producer_settings_fingerprint=_fp(f"settings.{segment_id}"),
    )


def _authorities(segments: tuple[SegmentDeclaration, ...]) -> tuple[AcceptedIntentAuthority, ...]:
    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def _spec(manifest: SegmentContextManifest) -> GenerationJobSpec:
    segment_id = manifest.segment_id
    return GenerationJobSpec(
        segment_id=segment_id,
        job_id=f"job.{segment_id}",
        graph_fingerprint=_fp(f"graph.{segment_id}"),
        compiled_prompt_fingerprint=_fp(f"compiled.{segment_id}"),
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui"),
        expected_format="video.mp4",
        expected_shape=(30, 512, 512, 3),
        timeout_ms=60_000,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )


def _receipt(
    manifest: SegmentContextManifest,
    spec: GenerationJobSpec,
    *,
    predecessor_output: str | None,
) -> SegmentArtifactReceipt:
    return SegmentArtifactReceipt(
        artifact_id=f"artifact.{manifest.segment_id}",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=manifest.workspace_id,
        workspace_revision=manifest.workspace_revision,
        workspace_fingerprint=manifest.workspace_fingerprint,
        segment_id=manifest.segment_id,
        manifest_fingerprint=manifest.fingerprint,
        producer_fingerprint=manifest.producer_fingerprint,
        transaction_fingerprint=_fp(f"transaction.{manifest.segment_id}"),
        graph_fingerprint=spec.graph_fingerprint,
        native_binding_fingerprint=manifest.native_binding_fingerprint,
        model_fingerprint=spec.model_fingerprint,
        runtime_fingerprint=spec.runtime_fingerprint,
        settings_fingerprint=manifest.producer_settings_fingerprint,
        source_id=manifest.source_id,
        predecessor_artifact_fingerprint=predecessor_output,
        execution_fingerprint=_fp(f"execution.{manifest.segment_id}"),
        format_label=spec.expected_format,
        shape=spec.expected_shape,
        created_at_ms=100,
        expires_at_ms=600_000,
        output_fingerprint=_fp(f"output.{manifest.segment_id}"),
        byte_length=4096,
    )


def _media(receipt: SegmentArtifactReceipt, *, inspected_at_ms: int = 300) -> AVMediaDescriptor:
    assert receipt.output_fingerprint is not None
    return AVMediaDescriptor(
        segment_id=receipt.segment_id,
        artifact_receipt_fingerprint=receipt.fingerprint,
        artifact_output_fingerprint=receipt.output_fingerprint,
        artifact_byte_length=receipt.byte_length,
        capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
        container="mp4",
        stream_count=2,
        video=AVVideoDescriptor(
            codec="h264",
            width=512,
            height=512,
            pixel_format="yuv420p",
            color_range="tv",
            color_space="bt709",
            color_primaries="bt709",
            color_transfer="bt709",
            rotation_degrees=0,
            frame_rate=AVRational(30, 1),
            time_base=AVRational(1, 90_000),
            start_time=AVRational(0, 1),
            end_time=AVRational(1, 1),
            decoded_frame_count=30,
        ),
        audio=AVAudioDescriptor(
            codec="aac",
            sample_format="fltp",
            sample_rate=48_000,
            channels=2,
            channel_layout="stereo",
            time_base=AVRational(1, 48_000),
            start_time=AVRational(0, 1),
            end_time=AVRational(1, 1),
            decoded_sample_count=48_000,
        ),
        subtitle_stream_count=0,
        data_stream_count=0,
        attachment_stream_count=0,
        inspected_at_ms=inspected_at_ms,
        expires_at_ms=inspected_at_ms + 60_000,
        warning_codes=(),
    )


def _target() -> AVTargetProfile:
    return AVTargetProfile(
        container="mp4",
        video_codec="h264",
        width=512,
        height=512,
        pixel_format="yuv420p",
        color_range="tv",
        color_space="bt709",
        color_primaries="bt709",
        color_transfer="bt709",
        rotation_degrees=0,
        frame_rate=AVRational(30, 1),
        video_time_base=AVRational(1, 90_000),
        audio_required=True,
        audio_codec="aac",
        sample_format="fltp",
        sample_rate=48_000,
        channels=2,
        channel_layout="stereo",
        audio_time_base=AVRational(1, 48_000),
    )


def _accepted_fixture(
    count: int = 2,
) -> tuple[
    SelectiveRerunPlan,
    SelectiveRerunApproval,
    GenerationSequenceState,
    tuple[SegmentArtifactReceipt, ...],
]:
    segments = tuple(
        _segment(
            f"segment.{index + 1}",
            predecessor=(f"segment.{index}" if index else None),
        )
        for index in range(count)
    )
    previous = create_workspace(
        "workspace.m17.11",
        segments,
        accepted_intent_authorities=_authorities(segments),
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=_authorities(segments),
        segments=segments,
    )
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(current)
    specs = tuple(_spec(item) for item in current_manifests)
    receipts: list[SegmentArtifactReceipt] = []
    for index, manifest in enumerate(previous_manifests):
        receipts.append(
            _receipt(
                manifest,
                specs[index],
                predecessor_output=(receipts[index - 1].output_fingerprint if index else None),
            )
        )
    evidence = tuple(
        SelectiveArtifactEvidence(
            segment_id=receipt.segment_id,
            status=ArtifactReuseStatus.REUSABLE,
            inspected_at_ms=200,
            receipt=receipt,
        )
        for receipt in receipts
    )
    selective = build_selective_rerun_plan(
        current,
        previous_manifests,
        current_manifests,
        requested_segment_ids=(),
        artifact_evidence=evidence,
        job_specs=specs,
    )
    selective_approval = SelectiveRerunApproval(selective.fingerprint, ())
    sequence = build_approved_generation_sequence_plan(
        selective,
        selective_approval,
        workspace=current,
        manifests=current_manifests,
        job_specs=specs,
        artifact_evidence=tuple(replace(item, inspected_at_ms=250) for item in evidence),
        approval_inspected_at_ms=250,
    )
    return (
        selective,
        selective_approval,
        create_generation_sequence_state(sequence),
        tuple(receipts),
    )


def _boundaries(
    receipts: tuple[SegmentArtifactReceipt, ...],
    *,
    workspace_id: str,
    workspace_revision: int,
    workspace_fingerprint: str,
) -> tuple[AVBoundaryEvidence, ...]:
    return tuple(
        AVBoundaryEvidence(
            workspace_id=workspace_id,
            workspace_revision=workspace_revision,
            workspace_fingerprint=workspace_fingerprint,
            predecessor_segment_id=receipts[index].segment_id,
            successor_segment_id=receipts[index + 1].segment_id,
            receipt=ContinuityBoundaryReceipt(
                boundary_id=f"boundary.{index + 1}",
                mode=ContinuityMode.CUT,
                audio_not_carried=True,
            ),
        )
        for index in range(len(receipts) - 1)
    )


def _plan_kwargs(count: int = 2) -> dict[str, Any]:
    selective, selective_approval, state, receipts = _accepted_fixture(count)
    boundaries = _boundaries(
        receipts,
        workspace_id=selective.workspace_id,
        workspace_revision=selective.workspace_revision,
        workspace_fingerprint=selective.workspace_fingerprint,
    )
    return {
        "selective_plan": selective,
        "selective_approval": selective_approval,
        "generation_state": state,
        "artifact_receipts": receipts,
        "media_descriptors": tuple(_media(item) for item in receipts),
        "boundary_evidence": boundaries,
        "accepted_boundary_receipt_fingerprints": tuple(
            item.receipt.fingerprint for item in boundaries
        ),
        "capability": qualified_ffmpeg_capability(),
        "limits": qualified_av_limits(),
        "target_profile": _target(),
        "planned_at_ms": 300,
    }


def test_exact_reused_join_is_deterministic_and_requires_exact_approval() -> None:
    kwargs = _plan_kwargs()
    state = cast(GenerationSequenceState, kwargs["generation_state"])

    first = build_av_reconstruction_plan(**kwargs)
    second = build_av_reconstruction_plan(**kwargs)

    assert first.to_wire() == second.to_wire()
    assert first.fingerprint == second.fingerprint
    assert tuple(item.segment_id for item in first.segments) == ("segment.1", "segment.2")
    assert len(first.boundaries) == 1
    assert state.plan.jobs == ()
    assert state.plan.clean_segment_ids == ("segment.1", "segment.2")

    approval = approve_av_reconstruction_plan(
        first,
        approved_at_ms=310,
        expires_at_ms=600,
    )
    approval.assert_executable(first, now_ms=599)
    with pytest.raises(AVReconstructionError, match="approval_stale"):
        approval.assert_executable(first, now_ms=600)
    with pytest.raises(AVReconstructionError, match="approval_plan_mismatch"):
        other_kwargs = _plan_kwargs()
        other_kwargs["planned_at_ms"] = 301
        approval.assert_executable(build_av_reconstruction_plan(**other_kwargs), now_ms=400)


def test_tampered_selective_plan_and_approval_identities_fail_closed() -> None:
    forged_plan = _plan_kwargs(count=1)
    forged_plan["selective_plan"] = replace(
        forged_plan["selective_plan"],
        plan_fingerprint=_fp("forged.selective.plan"),
    )
    with pytest.raises(AVReconstructionError, match="tampered_selective_plan"):
        build_av_reconstruction_plan(**forged_plan)

    forged_approval = _plan_kwargs(count=1)
    forged_approval["selective_approval"] = replace(
        forged_approval["selective_approval"],
        approval_fingerprint=_fp("forged.selective.approval"),
    )
    with pytest.raises(AVReconstructionError, match="tampered_selective_approval"):
        build_av_reconstruction_plan(**forged_approval)


def test_only_exact_gate_zero_capability_target_and_every_limit_are_admitted() -> None:
    for capability in (
        replace(qualified_ffmpeg_capability(), build_version="unqualified-build"),
        replace(qualified_ffmpeg_capability(), ffmpeg_sha256="0" * 64),
        replace(
            qualified_ffmpeg_capability(),
            operations=qualified_ffmpeg_capability().operations + (AVOperation.DROP_LEADING,),
        ),
    ):
        kwargs = _plan_kwargs(count=1)
        kwargs["capability"] = capability
        with pytest.raises(AVReconstructionError, match="adapter_capability_changed"):
            build_av_reconstruction_plan(**kwargs)

    target_drift = _plan_kwargs(count=1)
    target_drift["target_profile"] = replace(qualified_av_target_profile(), width=640)
    with pytest.raises(AVReconstructionError, match="adapter_capability_changed"):
        build_av_reconstruction_plan(**target_drift)

    for field in fields(type(qualified_av_limits())):
        limits = qualified_av_limits()
        object.__setattr__(limits, field.name, getattr(limits, field.name) + 1)
        kwargs = _plan_kwargs(count=1)
        kwargs["limits"] = limits
        with pytest.raises(AVReconstructionError, match="resource_limit_profile_changed"):
            build_av_reconstruction_plan(**kwargs)


def test_exported_plan_constructor_cannot_bypass_gate_zero_invariants() -> None:
    plan = build_av_reconstruction_plan(**_plan_kwargs())
    assert not hasattr(av_reconstruction_module, "_AV_PLAN_FACTORY_TOKEN")
    assert (
        av_reconstruction_module._mint_plan_attestation(
            plan._factory_attestation,
            plan.fingerprint,
        )
        is None
    )
    unqualified = replace(plan.capability, build_version="unqualified-build")
    drifted_limits = qualified_av_limits()
    object.__setattr__(drifted_limits, "approval_ttl_ms", drifted_limits.approval_ttl_ms + 1)
    drifted_target = replace(plan.target_profile, width=640)
    wrong_descriptor = replace(
        plan.segments[0],
        descriptor=replace(
            plan.segments[0].descriptor,
            capability_fingerprint=_fp("unrelated.capability"),
        ),
    )
    unsupported_operation = replace(
        plan.segments[0],
        operations=(AVOperation.DROP_LEADING,),
    )
    warning_descriptor = replace(
        plan.segments[0],
        descriptor=replace(plan.segments[0].descriptor, warning_codes=("vfr_detected",)),
    )
    cross_workspace_boundary = replace(plan.boundaries[0], workspace_id="workspace.unrelated")

    for changes in (
        {"capability": unqualified},
        {"limits": drifted_limits},
        {"target_profile": drifted_target},
        {"segments": (wrong_descriptor, plan.segments[1])},
        {"segments": (unsupported_operation, plan.segments[1])},
        {"segments": (warning_descriptor, plan.segments[1])},
        {"boundaries": (cross_workspace_boundary,)},
        {"selective_plan_fingerprint": _fp("forged.selective.plan")},
        {"selective_approval_fingerprint": _fp("forged.selective.approval")},
        {"generation_plan_fingerprint": _fp("forged.generation.plan")},
        {"generation_state_fingerprint": _fp("forged.generation.state")},
        {"manifest_fingerprints": (_fp("forged.manifest.1"), _fp("forged.manifest.2"))},
    ):
        with pytest.raises(AVReconstructionError, match="plan_factory_required"):
            replace(plan, **changes, plan_fingerprint=None)

    with pytest.raises(AVReconstructionError, match="plan_factory_required"):
        replace(
            plan,
            selective_plan_fingerprint=_fp("forged.with.exposed.attestation"),
            plan_fingerprint=None,
            _factory_token=plan._factory_attestation,
        )

    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    post_factory_drift = copy(plan)
    object.__setattr__(
        post_factory_drift,
        "selective_plan_fingerprint",
        _fp("post.factory.drift"),
    )
    with pytest.raises(AVReconstructionError, match="plan_factory_required"):
        approve_av_reconstruction_plan(post_factory_drift, approved_at_ms=310, expires_at_ms=600)
    with pytest.raises(AVReconstructionError, match="approval_plan_mismatch"):
        approval.assert_executable(post_factory_drift, now_ms=400)


@pytest.mark.parametrize(
    ("start", "end", "decoded_samples", "dropped", "inserted"),
    (
        (AVRational(0, 1), AVRational(1, 2), 24_000, 0, 24_000),
        (AVRational(0, 1), AVRational(3, 2), 72_000, 24_000, 0),
        (AVRational(1, 1), AVRational(2, 1), 48_000, 0, 0),
        (AVRational(1, 48_000), AVRational(48_001, 48_000), 48_000, 0, 0),
    ),
)
def test_audio_span_or_sample_mismatch_requires_explicit_normalization(
    start: AVRational,
    end: AVRational,
    decoded_samples: int,
    dropped: int,
    inserted: int,
) -> None:
    kwargs = _plan_kwargs(count=1)
    media = kwargs["media_descriptors"][0]
    assert media.audio is not None
    kwargs["media_descriptors"] = (
        replace(
            media,
            audio=replace(
                media.audio,
                start_time=start,
                end_time=end,
                decoded_sample_count=decoded_samples,
            ),
        ),
    )

    segment = build_av_reconstruction_plan(**kwargs).segments[0]

    assert segment.operations == (AVOperation.NORMALIZE_AUDIO,)
    assert segment.accounting.dropped_samples == dropped
    assert segment.accounting.inserted_samples == inserted


def test_cut_receipt_cannot_be_rebound_outside_accepted_boundary_mapping() -> None:
    kwargs = _plan_kwargs()
    boundary = kwargs["boundary_evidence"][0]
    accepted = (boundary.receipt.fingerprint,)
    unrelated = replace(
        boundary.receipt,
        boundary_id="boundary.unrelated",
        receipt_fingerprint=None,
    )
    kwargs["boundary_evidence"] = (replace(boundary, receipt=unrelated),)
    kwargs["accepted_boundary_receipt_fingerprints"] = accepted

    with pytest.raises(AVReconstructionError, match="boundary_identity_mismatch"):
        build_av_reconstruction_plan(**kwargs)


def test_native_boundary_requires_exact_accepted_successor_job_and_graph() -> None:
    kwargs = _plan_kwargs()
    receipts = kwargs["artifact_receipts"]
    predecessor = receipts[0]
    successor = receipts[1]
    assert predecessor.output_fingerprint is not None
    native = ContinuityBoundaryReceipt(
        boundary_id="boundary.native.unrelated",
        mode=ContinuityMode.NATIVE_FRAME_HANDOFF,
        audio_not_carried=True,
        predecessor_segment_id=predecessor.segment_id,
        predecessor_receipt_fingerprint=predecessor.fingerprint,
        predecessor_output_fingerprint=predecessor.output_fingerprint,
        successor_segment_id=successor.segment_id,
        successor_job_id="job.unrelated",
        successor_graph_fingerprint=_fp("graph.unrelated"),
        first_frame_node_instance_id="node.first.frame",
        first_frame_socket_name="first_frame",
        first_frame_connection_order=1,
        host_version="1.0.0",
        host_revision="0" * 40,
        video_node_source_sha256="1" * 64,
        video_types_source_sha256="2" * 64,
        decoder_name="synthetic_decoder",
        decoder_version="1.0.0",
        decoded_frame_count=30,
        carried_frame_count=1,
        delivered_frame_count=1,
        tail_content_sha256="3" * 64,
    )
    boundary = kwargs["boundary_evidence"][0]
    kwargs["boundary_evidence"] = (replace(boundary, receipt=native),)
    kwargs["accepted_boundary_receipt_fingerprints"] = (native.fingerprint,)

    with pytest.raises(AVReconstructionError, match="boundary_identity_mismatch"):
        build_av_reconstruction_plan(**kwargs)


def test_native_boundary_rejects_wrong_job_or_graph_for_dirty_successor() -> None:
    previous_segments = (
        _segment("segment.1"),
        _segment("segment.2", predecessor="segment.1"),
    )
    previous = create_workspace(
        "workspace.m17.11.native",
        previous_segments,
        accepted_intent_authorities=_authorities(previous_segments),
    )
    current_segments = (
        previous_segments[0],
        replace(
            previous_segments[1],
            producer_settings_fingerprint=_fp("settings.native.changed"),
        ),
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=_authorities(current_segments),
        segments=current_segments,
    )
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(current)
    specs = tuple(_spec(item) for item in current_manifests)
    predecessor = _receipt(previous_manifests[0], specs[0], predecessor_output=None)
    old_successor = _receipt(
        previous_manifests[1],
        specs[1],
        predecessor_output=predecessor.output_fingerprint,
    )
    evidence = tuple(
        SelectiveArtifactEvidence(
            segment_id=receipt.segment_id,
            status=ArtifactReuseStatus.REUSABLE,
            inspected_at_ms=200,
            receipt=receipt,
        )
        for receipt in (predecessor, old_successor)
    )
    selective = build_selective_rerun_plan(
        current,
        previous_manifests,
        current_manifests,
        requested_segment_ids=("segment.2",),
        artifact_evidence=evidence,
        job_specs=specs,
    )
    selective_approval = SelectiveRerunApproval(selective.fingerprint, ("segment.2",))
    sequence = build_approved_generation_sequence_plan(
        selective,
        selective_approval,
        workspace=current,
        manifests=current_manifests,
        job_specs=specs,
        artifact_evidence=(replace(evidence[0], inspected_at_ms=250),),
        approval_inspected_at_ms=250,
    )
    state = create_generation_sequence_state(sequence)
    successor_job = sequence.jobs[0]
    state = record_generation_projection(
        state,
        successor_job.job_id,
        transaction_id="transaction.m17.11.native",
        graph_fingerprint=successor_job.graph_fingerprint,
        compiled_prompt_fingerprint=specs[1].compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        successor_job.job_id,
        queue_prompt_id="queue.synthetic.native",
    )
    state = record_generation_running(
        state,
        successor_job.job_id,
        host_owner_id="host.synthetic.native",
    )
    transaction = state.runtime_for(successor_job.job_id).transaction
    assert transaction is not None
    successor = replace(
        _receipt(
            current_manifests[1],
            specs[1],
            predecessor_output=predecessor.output_fingerprint,
        ),
        transaction_fingerprint=transaction.fingerprint,
        receipt_fingerprint=None,
    )
    state = record_generation_success(
        state,
        successor_job.job_id,
        result_fingerprint=_fp("result.segment.2.native"),
        receipt=successor,
    )

    def native_receipt(*, job_id: str, graph_fingerprint: str) -> ContinuityBoundaryReceipt:
        assert predecessor.output_fingerprint is not None
        return ContinuityBoundaryReceipt(
            boundary_id="boundary.native.dirty",
            mode=ContinuityMode.NATIVE_FRAME_HANDOFF,
            audio_not_carried=True,
            predecessor_segment_id=predecessor.segment_id,
            predecessor_receipt_fingerprint=predecessor.fingerprint,
            predecessor_output_fingerprint=predecessor.output_fingerprint,
            successor_segment_id=successor.segment_id,
            successor_job_id=job_id,
            successor_graph_fingerprint=graph_fingerprint,
            first_frame_node_instance_id="node.first.frame",
            first_frame_socket_name="first_frame",
            first_frame_connection_order=1,
            host_version="1.0.0",
            host_revision="0" * 40,
            video_node_source_sha256="1" * 64,
            video_types_source_sha256="2" * 64,
            decoder_name="synthetic_decoder",
            decoder_version="1.0.0",
            decoded_frame_count=30,
            carried_frame_count=1,
            delivered_frame_count=1,
            tail_content_sha256="3" * 64,
        )

    for native in (
        native_receipt(job_id="job.unrelated", graph_fingerprint=successor_job.graph_fingerprint),
        native_receipt(job_id=successor_job.job_id, graph_fingerprint=_fp("graph.unrelated")),
    ):
        boundary = AVBoundaryEvidence(
            workspace_id=selective.workspace_id,
            workspace_revision=selective.workspace_revision,
            workspace_fingerprint=selective.workspace_fingerprint,
            predecessor_segment_id=predecessor.segment_id,
            successor_segment_id=successor.segment_id,
            receipt=native,
        )
        with pytest.raises(AVReconstructionError, match="boundary_identity_mismatch"):
            build_av_reconstruction_plan(
                selective_plan=selective,
                selective_approval=selective_approval,
                generation_state=state,
                artifact_receipts=(predecessor, successor),
                media_descriptors=(_media(predecessor), _media(successor)),
                boundary_evidence=(boundary,),
                accepted_boundary_receipt_fingerprints=(native.fingerprint,),
                capability=qualified_ffmpeg_capability(),
                limits=qualified_av_limits(),
                target_profile=_target(),
                planned_at_ms=300,
            )

    exact_native = native_receipt(
        job_id=successor_job.job_id,
        graph_fingerprint=successor_job.graph_fingerprint,
    )
    exact_boundary = AVBoundaryEvidence(
        workspace_id=selective.workspace_id,
        workspace_revision=selective.workspace_revision,
        workspace_fingerprint=selective.workspace_fingerprint,
        predecessor_segment_id=predecessor.segment_id,
        successor_segment_id=successor.segment_id,
        receipt=exact_native,
    )
    exact_plan = build_av_reconstruction_plan(
        selective_plan=selective,
        selective_approval=selective_approval,
        generation_state=state,
        artifact_receipts=(predecessor, successor),
        media_descriptors=(_media(predecessor), _media(successor)),
        boundary_evidence=(exact_boundary,),
        accepted_boundary_receipt_fingerprints=(exact_native.fingerprint,),
        capability=qualified_ffmpeg_capability(),
        limits=qualified_av_limits(),
        target_profile=_target(),
        planned_at_ms=300,
    )
    assert exact_plan.boundaries[0].receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF


def test_missing_or_reversed_boundary_fails_closed() -> None:
    base = _plan_kwargs()
    receipts = cast(tuple[SegmentArtifactReceipt, ...], base["artifact_receipts"])
    del base["boundary_evidence"]
    with pytest.raises(AVReconstructionError, match="boundary_missing"):
        build_av_reconstruction_plan(**base, boundary_evidence=())
    selective = cast(SelectiveRerunPlan, base["selective_plan"])
    boundary = _boundaries(
        receipts,
        workspace_id=selective.workspace_id,
        workspace_revision=selective.workspace_revision,
        workspace_fingerprint=selective.workspace_fingerprint,
    )[0]
    with pytest.raises(AVReconstructionError, match="boundary_identity_mismatch"):
        build_av_reconstruction_plan(
            **base,
            boundary_evidence=(
                replace(
                    boundary,
                    predecessor_segment_id=boundary.successor_segment_id,
                    successor_segment_id=boundary.predecessor_segment_id,
                ),
            ),
        )

    with pytest.raises(AVReconstructionError, match="boundary_identity_mismatch"):
        build_av_reconstruction_plan(
            **base,
            boundary_evidence=(
                replace(boundary, workspace_revision=boundary.workspace_revision + 1),
            ),
        )

    duplicate = _plan_kwargs(count=3)
    duplicate_boundaries = duplicate["boundary_evidence"]
    duplicate["boundary_evidence"] = (
        duplicate_boundaries[0],
        replace(duplicate_boundaries[1], receipt=duplicate_boundaries[0].receipt),
    )
    duplicate["accepted_boundary_receipt_fingerprints"] = (
        duplicate_boundaries[0].receipt.fingerprint,
        duplicate_boundaries[0].receipt.fingerprint,
    )
    with pytest.raises(AVReconstructionError, match="duplicate_plan.boundary_receipts"):
        build_av_reconstruction_plan(**duplicate)


def test_zero_maximum_and_over_limit_segment_bounds() -> None:
    one = build_av_reconstruction_plan(**_plan_kwargs(count=1))
    with pytest.raises(AVReconstructionError, match="plan_factory_required"):
        replace(
            one,
            artifact_receipt_fingerprints=(),
            boundary_receipt_fingerprints=(),
            segments=(),
            boundaries=(),
            plan_fingerprint=None,
        )

    maximum = build_av_reconstruction_plan(**_plan_kwargs(count=64))
    assert len(maximum.segments) == 64
    assert len(maximum.boundaries) == 63

    with pytest.raises(AVReconstructionError, match="limits.segment_ceiling"):
        replace(qualified_av_limits(), max_segments=65, max_boundaries=64)


def test_stale_inspection_and_extra_streams_fail_before_planning() -> None:
    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    base["media_descriptors"] = (replace(media, expires_at_ms=301),)
    base["planned_at_ms"] = 301
    with pytest.raises(AVReconstructionError, match="inspection_stale"):
        build_av_reconstruction_plan(**base)

    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    base["media_descriptors"] = (replace(media, stream_count=3, subtitle_stream_count=1),)
    with pytest.raises(AVReconstructionError, match="operation_unsupported:extra_stream"):
        build_av_reconstruction_plan(**base)

    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    assert media.video is not None
    base["media_descriptors"] = (
        replace(media, video=replace(media.video, color_space="bt2020nc")),
    )
    with pytest.raises(AVReconstructionError, match="operation_unsupported:color"):
        build_av_reconstruction_plan(**base)

    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    base["media_descriptors"] = (replace(media, warning_codes=("vfr_detected",)),)
    with pytest.raises(AVReconstructionError, match="inspection_invalid"):
        build_av_reconstruction_plan(**base)

    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    assert media.video is not None
    with pytest.raises(AVReconstructionError, match="video_frame_rate_count_mismatch"):
        replace(media.video, decoded_frame_count=29)
    assert media.audio is not None
    with pytest.raises(AVReconstructionError, match="audio_rate_count_mismatch"):
        replace(media.audio, decoded_sample_count=47_999)


def test_supported_mismatch_has_closed_operations_and_exact_accounting() -> None:
    base = _plan_kwargs(count=1)
    media = base["media_descriptors"][0]
    assert media.video is not None
    base["media_descriptors"] = (
        replace(
            media,
            stream_count=1,
            video=replace(media.video, width=640),
            audio=None,
        ),
    )

    plan = build_av_reconstruction_plan(**base)

    segment = plan.segments[0]
    assert segment.operations == (
        AVOperation.NORMALIZE_VIDEO,
        AVOperation.INSERT_SILENCE,
    )
    assert segment.accounting.decoded_samples == 0
    assert segment.accounting.inserted_samples == 48_000
    assert segment.accounting.emitted_samples == 48_000

    remux = _plan_kwargs(count=1)
    remux_media = remux["media_descriptors"][0]
    remux["media_descriptors"] = (replace(remux_media, container="matroska"),)
    with pytest.raises(AVReconstructionError, match="operation_unsupported:container"):
        build_av_reconstruction_plan(**remux)

    normalize_audio = _plan_kwargs(count=1)
    audio_media = normalize_audio["media_descriptors"][0]
    assert audio_media.audio is not None
    normalize_audio["media_descriptors"] = (
        replace(
            audio_media,
            audio=replace(
                audio_media.audio,
                sample_rate=96_000,
                time_base=AVRational(1, 96_000),
                decoded_sample_count=96_000,
            ),
        ),
    )
    audio_plan = build_av_reconstruction_plan(**normalize_audio)
    assert audio_plan.segments[0].operations == (AVOperation.NORMALIZE_AUDIO,)
    capability = qualified_ffmpeg_capability()
    assert capability.input_containers == ("mp4",)
    assert capability.video_decoders == ("h264",)
    assert capability.audio_decoders == ("aac", "pcm_s16le")
    assert AVOperation.REMUX not in capability.operations
    assert AVOperation.DROP_LEADING not in capability.operations
    assert AVOperation.DROP_TRAILING not in capability.operations


def test_dirty_segment_requires_exact_terminal_generation_receipt() -> None:
    previous_segment = _segment("segment.1")
    previous = create_workspace(
        "workspace.m17.11.dirty",
        (previous_segment,),
        accepted_intent_authorities=_authorities((previous_segment,)),
    )
    current_segment = replace(
        previous_segment,
        producer_settings_fingerprint=_fp("settings.changed"),
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=_authorities((current_segment,)),
        segments=(current_segment,),
    )
    previous_manifests = derive_segment_manifests(previous)
    current_manifests = derive_segment_manifests(current)
    spec = _spec(current_manifests[0])
    selective = build_selective_rerun_plan(
        current,
        previous_manifests,
        current_manifests,
        job_specs=(spec,),
    )
    selective_approval = SelectiveRerunApproval(
        selective.fingerprint,
        ("segment.1",),
    )
    sequence = build_approved_generation_sequence_plan(
        selective,
        selective_approval,
        workspace=current,
        manifests=current_manifests,
        job_specs=(spec,),
    )
    state = create_generation_sequence_state(sequence)
    state = record_generation_projection(
        state,
        spec.job_id,
        transaction_id="transaction.m17.11.dirty",
        graph_fingerprint=spec.graph_fingerprint,
        compiled_prompt_fingerprint=spec.compiled_prompt_fingerprint,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    state = record_generation_submission(
        state,
        spec.job_id,
        queue_prompt_id="queue.synthetic",
    )
    state = record_generation_running(
        state,
        spec.job_id,
        host_owner_id="host.synthetic",
    )
    transaction = state.runtime_for(spec.job_id).transaction
    assert transaction is not None
    receipt = replace(
        _receipt(current_manifests[0], spec, predecessor_output=None),
        transaction_fingerprint=transaction.fingerprint,
        receipt_fingerprint=None,
    )
    state = record_generation_success(
        state,
        spec.job_id,
        result_fingerprint=_fp("result.segment.1"),
        receipt=receipt,
    )

    plan = build_av_reconstruction_plan(
        selective_plan=selective,
        selective_approval=selective_approval,
        generation_state=state,
        artifact_receipts=(receipt,),
        media_descriptors=(_media(receipt),),
        boundary_evidence=(),
        accepted_boundary_receipt_fingerprints=(),
        capability=qualified_ffmpeg_capability(),
        limits=qualified_av_limits(),
        target_profile=_target(),
        planned_at_ms=300,
    )

    assert plan.segments[0].disposition.value == "queue"
    assert plan.artifact_receipt_fingerprints == (receipt.fingerprint,)


def test_sole_receipt_decoder_is_strict_canonical_and_locator_free() -> None:
    receipt = AVReconstructionReceipt(
        transaction_id="transaction.m17.11",
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
                handle="avout_0123456789abcdef0123456789abcdef",
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=8192,
                content_fingerprint=_fp("output"),
                video_frame_count=30,
                audio_sample_count=48_000,
                duration=AVRational(1, 1),
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=500,
    )
    encoded = receipt.to_wire_bytes()
    decoded = decode_av_reconstruction_receipt(encoded)

    assert decoded == receipt
    assert encoded == canonical_bytes(decoded.to_wire())
    schema_path = (
        Path(__file__).parents[1]
        / "governance"
        / "contracts"
        / "av_reconstruction_receipt_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(decoded.to_wire())
    public_text = json.dumps(decoded.to_public_dict(), sort_keys=True)
    for forbidden in ("path", "command", "stdout", "stderr", "prompt", "credential", "http"):
        assert forbidden not in public_text.lower()

    unknown = json.loads(encoded)
    unknown["unknown_member"] = "forbidden"
    with pytest.raises(AVReconstructionError, match="receipt_members"):
        decode_av_reconstruction_receipt(json.dumps(unknown))
    duplicate = encoded.decode("utf-8").replace(
        '"transaction_id":"transaction.m17.11"',
        '"transaction_id":"transaction.m17.11","transaction_id":"transaction.other"',
    )
    with pytest.raises(AVReconstructionError, match="duplicate_receipt_member"):
        decode_av_reconstruction_receipt(duplicate)

    noncanonical = json.dumps(json.loads(encoded), indent=2, sort_keys=True)
    with pytest.raises(AVReconstructionError, match="receipt_not_canonical"):
        decode_av_reconstruction_receipt(noncanonical)

    legacy = json.loads(encoded)
    legacy["schema"] = "h3.context.local_reconstruction.v1"
    with pytest.raises(AVReconstructionError, match="unsupported_receipt_schema"):
        decode_av_reconstruction_receipt(json.dumps(legacy, separators=(",", ":"), sort_keys=True))

    tampered = json.loads(encoded)
    tampered["segment_results"][0]["accounting"]["decoded_samples"] = 47_999
    with pytest.raises(AVReconstructionError, match="audio_decode_accounting"):
        decode_av_reconstruction_receipt(
            json.dumps(tampered, separators=(",", ":"), sort_keys=True)
        )
