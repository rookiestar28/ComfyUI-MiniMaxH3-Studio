"""Focused M17-10 tests for the single receipt and qualified native boundary."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.adapters import comfyui_continuity as continuity_adapter
from comfyui_h3_context.adapters.comfyui_continuity import (
    DecodeResult,
    DecodeWorker,
    extract_native_tail,
)
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core import (
    CONTINUITY_BOUNDARY_RECEIPT_SCHEMA,
    QUALIFIED_COMFYUI_HOST_REVISION,
    QUALIFIED_COMFYUI_HOST_VERSION,
    QUALIFIED_DECODER_NAME,
    QUALIFIED_DECODER_VERSION,
    QUALIFIED_VIDEO_NODE_SOURCE_SHA256,
    QUALIFIED_VIDEO_TYPES_SOURCE_SHA256,
    ContinuityAdmissionError,
    ContinuityBindingError,
    ContinuityBoundaryReceipt,
    ContinuityCancelledError,
    ContinuityCapability,
    ContinuityError,
    ContinuityLimits,
    ContinuityMode,
    ContinuityPolicy,
    ContinuityReceiptError,
    ContinuityRuntimeTail,
    ContinuityTimeoutError,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequencePlan,
    GraphAnchor,
    GraphAnchorRole,
    GraphNode,
    NativeGraphMaterialization,
    NativeTailBindingEvidence,
    NativeTailExtractionEvidence,
    SegmentArtifactReceipt,
    SegmentContextManifest,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    TaskMode,
    VideoAdmissionMetadata,
    assert_accepted_m17_08_successor_identity,
    build_continuity_boundary_receipt,
    build_continuity_policy,
    build_generation_sequence_plan,
    canonical_bytes,
    create_workspace,
    decode_continuity_boundary_receipt,
    derive_segment_manifests,
    estimate_tensor_bytes,
    plan_recompute,
    revise_workspace,
    validate_predecessor_artifact_admission,
)
from comfyui_h3_context.core.graph_binding import VisibleGraph
from comfyui_h3_context.core.segment_artifacts import ArtifactLifecycleState


def _fp(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode('ascii')).hexdigest()}"


def _capability() -> ContinuityCapability:
    return ContinuityCapability(
        host_version=QUALIFIED_COMFYUI_HOST_VERSION,
        host_revision=QUALIFIED_COMFYUI_HOST_REVISION,
        video_node_source_sha256=QUALIFIED_VIDEO_NODE_SOURCE_SHA256,
        video_types_source_sha256=QUALIFIED_VIDEO_TYPES_SOURCE_SHA256,
        decoder_name=QUALIFIED_DECODER_NAME,
        decoder_version=QUALIFIED_DECODER_VERSION,
    )


def _segment(
    segment_id: str,
    mode: TaskMode,
    *,
    settings: str,
    relation: SegmentRelationKind = SegmentRelationKind.INDEPENDENT,
    predecessor: str | None = None,
) -> SegmentDeclaration:
    return SegmentDeclaration(
        segment_id=segment_id,
        task_mode=mode,
        source_id=f"source.{segment_id}",
        reference_ids=() if mode is TaskMode.T2VA else (f"reference.{segment_id}",),
        duration=SegmentDuration.from_frame_count(5),
        relation=relation,
        predecessor_segment_id=predecessor,
        accepted_intent_fingerprint=_fp(f"intent.{segment_id}"),
        semantic_receipt_fingerprint=None,
        profile_fingerprint=_fp(f"profile.{mode.value}"),
        reference_registry_fingerprint=_fp(f"registry.{segment_id}"),
        native_binding_fingerprint=_fp(f"native.{mode.value}"),
        producer_settings_fingerprint=_fp(settings),
    )


def _authorities(segments: tuple[SegmentDeclaration, ...]) -> tuple[Any, ...]:
    from comfyui_h3_context.core.segment_workspace import AcceptedIntentAuthority

    return tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in segments
    )


def _successor_fixture(
    mode: TaskMode,
    *,
    first_frame_as_output: bool = False,
) -> tuple[
    SegmentContextManifest,
    SegmentArtifactReceipt,
    GenerationSequencePlan,
    VisibleGraph,
    SegmentContextManifest,
]:
    previous_segments = (
        _segment("segment.pred", TaskMode.T2VA, settings="settings.pred"),
        _segment(
            "segment.next",
            mode,
            settings="settings.old",
            relation=SegmentRelationKind.PREDECESSOR,
            predecessor="segment.pred",
        ),
    )
    previous = create_workspace(
        "workspace.m17.10",
        previous_segments,
        accepted_intent_authorities=_authorities(previous_segments),
    )
    current_segments = (
        previous_segments[0],
        replace(previous_segments[1], producer_settings_fingerprint=_fp("settings.new")),
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=_authorities(current_segments),
        segments=current_segments,
    )
    manifests = derive_segment_manifests(current)
    predecessor_manifest = manifests[0]
    successor_manifest = manifests[1]
    payload = b"accepted-video-payload"
    predecessor_receipt = SegmentArtifactReceipt(
        artifact_id="artifact.segment.pred",
        state=ArtifactLifecycleState.COMPLETE,
        workspace_id=predecessor_manifest.workspace_id,
        workspace_revision=predecessor_manifest.workspace_revision,
        workspace_fingerprint=predecessor_manifest.workspace_fingerprint,
        segment_id=predecessor_manifest.segment_id,
        manifest_fingerprint=predecessor_manifest.fingerprint,
        producer_fingerprint=predecessor_manifest.producer_fingerprint,
        transaction_fingerprint=_fp("transaction.pred"),
        graph_fingerprint=_fp("graph.pred"),
        native_binding_fingerprint=predecessor_manifest.native_binding_fingerprint,
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        settings_fingerprint=predecessor_manifest.producer_settings_fingerprint,
        source_id=predecessor_manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp("execution.pred"),
        format_label="video.mp4",
        shape=(3, 32, 32, 3),
        created_at_ms=1_000,
        expires_at_ms=100_000,
        output_fingerprint=_fp(payload.decode("ascii")),
        byte_length=len(payload),
    )
    node = GraphNode(
        node_instance_id="node.successor",
        node_id="MiniMaxH3ImageToVideo",
        input_ports=("last_frame",) if first_frame_as_output else ("first_frame", "last_frame"),
        output_ports=("first_frame", "conditioning", "latent")
        if first_frame_as_output
        else ("conditioning", "latent"),
    )
    graph = VisibleGraph(
        fixture_id="fixture.m17.08.successor",
        task_modes=(mode,),
        nodes=(node,),
        anchors=(
            GraphAnchor(GraphAnchorRole.FIRST_FRAME, node.node_instance_id, "first_frame", 1),
            *(
                (GraphAnchor(GraphAnchorRole.LAST_FRAME, node.node_instance_id, "last_frame", 2),)
                if mode is TaskMode.FL2VA
                else ()
            ),
        ),
    )
    spec = GenerationJobSpec(
        segment_id=successor_manifest.segment_id,
        job_id="job.segment.next",
        graph_fingerprint=graph.fingerprint,
        compiled_prompt_fingerprint=_fp("compiled.next"),
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        expected_format="video.mp4",
        expected_shape=(3, 32, 32, 3),
        timeout_ms=60_000,
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    recompute = plan_recompute(
        derive_segment_manifests(previous),
        manifests,
    )
    plan = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        (spec,),
        reusable_receipts=(predecessor_receipt,),
    )
    return predecessor_manifest, predecessor_receipt, plan, graph, successor_manifest


def _extraction(receipt: SegmentArtifactReceipt) -> NativeTailExtractionEvidence:
    tail_bytes = b"tail-runtime-bytes"
    metadata = VideoAdmissionMetadata(
        width=32,
        height=32,
        declared_frame_count=3,
        duration_ms=1_500,
        frames_per_second=2.0,
        estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
    )
    tail = ContinuityRuntimeTail(
        tensor=object(),
        shape=(1, 32, 32, 3),
        content_sha256=hashlib.sha256(tail_bytes).hexdigest(),
    )
    return NativeTailExtractionEvidence(
        predecessor_receipt_fingerprint=receipt.fingerprint,
        predecessor_output_fingerprint=cast(str, receipt.output_fingerprint),
        input_byte_length=23,
        admission=metadata,
        decoded_frame_count=3,
        carried_frame_count=1,
        delivered_frame_count=1,
        tail=tail,
        capability=_capability(),
    )


def _native_receipt(
    mode: TaskMode,
) -> tuple[ContinuityBoundaryReceipt, NativeTailExtractionEvidence, VisibleGraph]:
    predecessor_manifest, predecessor_receipt, plan, graph, successor_manifest = _successor_fixture(
        mode
    )
    job = plan.job_for_id("job.segment.next")
    extraction = _extraction(predecessor_receipt)
    last_frame = None if mode is TaskMode.I2VA else object()

    def materialize(
        tail: ContinuityRuntimeTail,
        anchor: GraphAnchor,
        successor_job: Any,
    ) -> NativeGraphMaterialization:
        return NativeGraphMaterialization(
            job_id=successor_job.job_id,
            graph_fingerprint=graph.fingerprint,
            node_instance_id=anchor.node_instance_id,
            socket_name=anchor.socket_name,
            connection_order=anchor.connection_order,
            bound_tail=tail,
            bound_input=tail.tensor,
            last_frame_after=last_frame,
        )

    binding = continuity_binding(
        extraction,
        plan,
        job,
        successor_manifest,
        graph,
        last_frame,
        materialize,
    )
    policy = build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, mode)
    return (
        build_continuity_boundary_receipt(
            policy,
            boundary_id="boundary.m17.10",
            predecessor_manifest=predecessor_manifest,
            predecessor_receipt=predecessor_receipt,
            successor_plan=plan,
            successor_job=job,
            successor_manifest=successor_manifest,
            binding=binding,
        ),
        extraction,
        graph,
    )


def continuity_binding(
    extraction: NativeTailExtractionEvidence,
    plan: GenerationSequencePlan,
    job: Any,
    manifest: SegmentContextManifest,
    graph: VisibleGraph,
    last_frame: object | None,
    materialize: Any,
) -> NativeTailBindingEvidence:
    from comfyui_h3_context.core.continuity_handoff import bind_native_tail_to_successor

    return bind_native_tail_to_successor(
        extraction,
        successor_plan=plan,
        successor_job=job,
        successor_manifest=manifest,
        visible_graph=graph,
        last_frame_before=last_frame,
        materialize=materialize,
    )


def _store_with_receipt(
    tmp_path: Path,
    receipt: SegmentArtifactReceipt,
    payload: bytes,
) -> tuple[PrivateSegmentArtifactStore, SegmentArtifactReceipt]:
    store = PrivateSegmentArtifactStore(tmp_path, clock_ms=lambda: 1_000)
    partial = replace(
        receipt,
        state=ArtifactLifecycleState.PARTIAL,
        output_fingerprint=None,
        byte_length=0,
        receipt_fingerprint=None,
    )
    store.begin(partial)
    return store, store.commit(partial, payload)


def test_mode_matrix_is_closed_and_cut_restart_are_explicit() -> None:
    assert (
        build_continuity_policy(
            ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.I2VA
        ).preserves_last_frame
        is False
    )
    assert (
        build_continuity_policy(
            ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.FL2VA
        ).preserves_last_frame
        is True
    )
    with pytest.raises(ContinuityError):
        build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.T2VA)
    with pytest.raises(ContinuityError):
        build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.L2VA)
    with pytest.raises(ContinuityError):
        build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.REF2VA)
    with pytest.raises(ContinuityReceiptError):
        build_continuity_boundary_receipt(
            ContinuityPolicy(ContinuityMode.CUT, TaskMode.T2VA),
            boundary_id="boundary.cut",
            restart_root_id="root.not.allowed",
        )
    restart = build_continuity_boundary_receipt(
        ContinuityPolicy(ContinuityMode.RESTART, TaskMode.REF2VA),
        boundary_id="boundary.restart",
        restart_root_id="root.new",
    )
    assert restart.to_wire() == {
        "schema": CONTINUITY_BOUNDARY_RECEIPT_SCHEMA,
        "boundary_id": "boundary.restart",
        "continuity_mode": "restart",
        "audio_not_carried": True,
        "restart_root_id": "root.new",
        "receipt_fingerprint": restart.fingerprint,
    }


def test_limits_accept_boundary_and_reject_boundary_plus_one() -> None:
    limits = ContinuityLimits(
        max_input_bytes=10,
        max_width=32,
        max_height=32,
        max_decoded_frame_count=3,
        max_aggregate_pixels=3 * 32 * 32,
        max_estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
        max_duration_ms=1_500,
        max_fps=2.0,
    )
    metadata = VideoAdmissionMetadata(
        width=32,
        height=32,
        declared_frame_count=3,
        duration_ms=1_500,
        frames_per_second=2.0,
        estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
    )
    limits.validate_admission(metadata, 10)
    with pytest.raises(ContinuityAdmissionError):
        limits.validate_admission(metadata, 11)
    with pytest.raises(ContinuityAdmissionError):
        limits.validate_admission(replace(metadata, width=33), 10)
    with pytest.raises(ContinuityAdmissionError):
        limits.validate_admission(replace(metadata, declared_frame_count=4), 10)
    with pytest.raises(ContinuityAdmissionError):
        limits.validate_admission(replace(metadata, duration_ms=1_501), 10)
    with pytest.raises(ContinuityAdmissionError):
        limits.validate_admission(replace(metadata, frames_per_second=2.1), 10)
    with pytest.raises(ContinuityAdmissionError):
        ContinuityLimits(max_aggregate_pixels=3 * 32 * 32 - 1).validate_admission(metadata, 10)
    with pytest.raises(ContinuityAdmissionError):
        ContinuityLimits(
            max_estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32) - 1
        ).validate_admission(metadata, 10)


def test_binding_rejects_first_frame_output_anchor() -> None:
    _, predecessor_receipt, plan, graph, successor_manifest = _successor_fixture(
        TaskMode.I2VA, first_frame_as_output=True
    )
    extraction = _extraction(predecessor_receipt)
    job = plan.job_for_id("job.segment.next")
    with pytest.raises(ContinuityBindingError, match="first_frame_anchor_not_input"):
        continuity_binding(
            extraction,
            plan,
            job,
            successor_manifest,
            graph,
            None,
            lambda tail, anchor, successor_job: NativeGraphMaterialization(
                job_id=successor_job.job_id,
                graph_fingerprint=graph.fingerprint,
                node_instance_id=anchor.node_instance_id,
                socket_name=anchor.socket_name,
                connection_order=anchor.connection_order,
                bound_tail=tail,
                bound_input=tail.tensor,
                last_frame_after=None,
            ),
        )


def test_native_receipt_requires_actual_binding_and_preserves_fl2va_last_frame() -> None:
    receipt, extraction, graph = _native_receipt(TaskMode.FL2VA)
    assert receipt.mode is ContinuityMode.NATIVE_FRAME_HANDOFF
    assert receipt.carried_frame_count == 1
    assert receipt.delivered_frame_count == 1
    assert receipt.audio_not_carried is True
    assert receipt.first_frame_socket_name == "first_frame"
    assert "audio_sample_count" not in receipt.to_wire()
    assert "affected_segment_ids" not in receipt.to_wire()
    assert "m17_09_disposition" not in receipt.to_wire()
    assert not hasattr(extraction, "to_public_dict")
    assert not hasattr(extraction.tail, "to_public_dict")
    assert not hasattr(receipt, "to_public_dict")
    assert graph.fingerprint == receipt.successor_graph_fingerprint

    predecessor_manifest, predecessor_receipt, plan, visible_graph, successor_manifest = (
        _successor_fixture(TaskMode.FL2VA)
    )
    job = plan.job_for_id("job.segment.next")
    last_frame = object()

    def changed_last_frame(
        tail: ContinuityRuntimeTail,
        anchor: GraphAnchor,
        successor_job: Any,
    ) -> NativeGraphMaterialization:
        return NativeGraphMaterialization(
            job_id=successor_job.job_id,
            graph_fingerprint=visible_graph.fingerprint,
            node_instance_id=anchor.node_instance_id,
            socket_name=anchor.socket_name,
            connection_order=anchor.connection_order,
            bound_tail=tail,
            bound_input=tail.tensor,
            last_frame_after=object(),
        )

    with pytest.raises(ContinuityBindingError):
        continuity_binding(
            _extraction(predecessor_receipt),
            plan,
            job,
            successor_manifest,
            visible_graph,
            last_frame,
            changed_last_frame,
        )


def test_strict_receipt_decoder_and_schema_have_one_native_contract() -> None:
    receipt, _, _ = _native_receipt(TaskMode.I2VA)
    payload = receipt.to_wire_bytes()
    assert decode_continuity_boundary_receipt(payload) == receipt
    schema_path = (
        Path(__file__).parents[1]
        / "governance"
        / "contracts"
        / "continuity_boundary_receipt_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt.to_wire())
    assert set(receipt.to_wire()) == {
        "schema",
        "boundary_id",
        "continuity_mode",
        "audio_not_carried",
        "receipt_fingerprint",
        "predecessor_segment_id",
        "predecessor_receipt_fingerprint",
        "predecessor_output_fingerprint",
        "successor_segment_id",
        "successor_job_id",
        "successor_graph_fingerprint",
        "first_frame_node_instance_id",
        "first_frame_socket_name",
        "first_frame_connection_order",
        "host_version",
        "host_revision",
        "video_node_source_sha256",
        "video_types_source_sha256",
        "decoder_name",
        "decoder_version",
        "decoded_frame_count",
        "carried_frame_count",
        "delivered_frame_count",
        "tail_content_sha256",
    }
    unknown = dict(receipt.to_wire())
    unknown["unexpected"] = True
    with pytest.raises(ContinuityReceiptError):
        decode_continuity_boundary_receipt(canonical_bytes(unknown))
    tampered = dict(receipt.to_wire())
    tampered["receipt_fingerprint"] = _fp("tampered")
    with pytest.raises(ContinuityReceiptError):
        decode_continuity_boundary_receipt(canonical_bytes(tampered))
    with pytest.raises(ContinuityReceiptError):
        decode_continuity_boundary_receipt(b'{"schema":"x","schema":"y"}')
    with pytest.raises(ContinuityReceiptError):
        decode_continuity_boundary_receipt(json.dumps(receipt.to_wire()).encode("utf-8"))


def test_native_extraction_reads_only_reusable_store_and_reports_actual_counts(
    tmp_path: Path,
) -> None:
    predecessor_manifest, predecessor_receipt, _, _, _ = _successor_fixture(TaskMode.I2VA)
    payload = b"accepted-video-payload"
    store, committed = _store_with_receipt(tmp_path / "store", predecessor_receipt, payload)
    metadata = VideoAdmissionMetadata(
        width=32,
        height=32,
        declared_frame_count=3,
        duration_ms=1_500,
        frames_per_second=2.0,
        estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
    )
    tail_bytes = bytes(estimate_tensor_bytes(1, 32, 32))
    evidence = extract_native_tail(
        store,
        predecessor_manifest,
        committed,
        ContinuityLimits(),
        _capability(),
        metadata_probe=lambda value: metadata,
        worker=lambda value, limits, cancelled: DecodeResult(
            tail_bytes=tail_bytes,
            shape=(1, 32, 32, 3),
            decoded_frame_count=3,
        ),
        tensor_factory=lambda value, shape: ("runtime-image", shape),
        test_only_seams=True,
    )
    assert evidence.input_byte_length == len(payload)
    assert evidence.decoded_frame_count == 3
    assert evidence.carried_frame_count == 1
    assert evidence.delivered_frame_count == 1
    assert evidence.audio_not_carried is True
    assert evidence.tail.shape == (1, 32, 32, 3)
    with pytest.raises(ContinuityAdmissionError):
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(max_input_bytes=len(payload) - 1),
            _capability(),
            metadata_probe=lambda value: metadata,
            worker=lambda value, limits, cancelled: DecodeResult(
                tail_bytes=tail_bytes,
                shape=(1, 32, 32, 3),
                decoded_frame_count=3,
            ),
            tensor_factory=lambda value, shape: object(),
            test_only_seams=True,
        )


def test_store_admission_rejects_missing_partial_expired_and_tampered(tmp_path: Path) -> None:
    predecessor_manifest, predecessor_receipt, _, _, _ = _successor_fixture(TaskMode.I2VA)
    payload = b"accepted-video-payload"

    missing_root = tmp_path / "missing"
    missing_store, missing = _store_with_receipt(missing_root, predecessor_receipt, payload)
    (missing_root / "artifacts" / f"{missing.artifact_id}.bin").unlink()
    with pytest.raises(ContinuityAdmissionError, match="artifact_not_reusable"):
        extract_native_tail(
            missing_store,
            predecessor_manifest,
            missing,
            ContinuityLimits(),
            _capability(),
        )

    partial = replace(
        missing,
        state=ArtifactLifecycleState.PARTIAL,
        output_fingerprint=None,
        byte_length=0,
        receipt_fingerprint=None,
    )
    with pytest.raises(ContinuityBindingError, match="predecessor_not_complete"):
        validate_predecessor_artifact_admission(partial, predecessor_manifest)

    expired_root = tmp_path / "expired"
    expired_store, expired = _store_with_receipt(expired_root, predecessor_receipt, payload)
    expired_store = PrivateSegmentArtifactStore(expired_root, clock_ms=lambda: 100_001)
    with pytest.raises(ContinuityAdmissionError, match="artifact_not_reusable"):
        extract_native_tail(
            expired_store,
            predecessor_manifest,
            expired,
            ContinuityLimits(),
            _capability(),
        )

    tampered_root = tmp_path / "tampered"
    tampered_store, tampered = _store_with_receipt(tampered_root, predecessor_receipt, payload)
    (tampered_root / "artifacts" / f"{tampered.artifact_id}.bin").write_bytes(b"tampered")
    with pytest.raises(ContinuityAdmissionError, match="artifact_not_reusable"):
        extract_native_tail(
            tampered_store,
            predecessor_manifest,
            tampered,
            ContinuityLimits(),
            _capability(),
        )

    with pytest.raises(ContinuityBindingError, match="predecessor_receipt_type"):
        validate_predecessor_artifact_admission(cast(Any, object()), predecessor_manifest)


def test_capability_drift_fails_before_store_decode(tmp_path: Path) -> None:
    predecessor_manifest, predecessor_receipt, _, _, _ = _successor_fixture(TaskMode.I2VA)
    store, committed = _store_with_receipt(
        tmp_path / "store", predecessor_receipt, b"accepted-video-payload"
    )
    with pytest.raises(ContinuityAdmissionError, match="capability_drift"):
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(),
            replace(_capability(), video_node_source_sha256="0" * 64),
        )


def test_host_identity_is_provenance_for_an_unchanged_continuity_capability() -> None:
    replace(
        _capability(),
        host_version="0.33.0",
        host_revision="0000000000000000000000000000000000000000",
    ).assert_qualified()


def test_test_seams_are_explicit_and_failure_context_is_sanitized(tmp_path: Path) -> None:
    predecessor_manifest, predecessor_receipt, _, _, _ = _successor_fixture(TaskMode.I2VA)
    payload = b"accepted-video-payload"
    store, committed = _store_with_receipt(tmp_path / "store", predecessor_receipt, payload)
    with pytest.raises(ContinuityAdmissionError) as disabled:
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(),
            _capability(),
            metadata_probe=lambda value: VideoAdmissionMetadata(
                width=32,
                height=32,
                declared_frame_count=3,
                duration_ms=1_500,
                frames_per_second=2.0,
                estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
            ),
            worker=lambda value, limits, cancelled: DecodeResult(
                tail_bytes=bytes(estimate_tensor_bytes(1, 32, 32)),
                shape=(1, 32, 32, 3),
                decoded_frame_count=3,
            ),
        )
    assert str(disabled.value) == "test_seams_disabled"
    private_path = r"C:\private\secret.mp4"
    with pytest.raises(ContinuityAdmissionError) as sanitized:
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(),
            _capability(),
            metadata_probe=lambda value: (_ for _ in ()).throw(RuntimeError(private_path)),
            worker=lambda value, limits, cancelled: DecodeResult(
                tail_bytes=bytes(estimate_tensor_bytes(1, 32, 32)),
                shape=(1, 32, 32, 3),
                decoded_frame_count=3,
            ),
            test_only_seams=True,
        )
    assert str(sanitized.value) == "video_metadata_unavailable"
    assert sanitized.value.__cause__ is None
    assert sanitized.value.__suppress_context__ is True
    assert private_path not in repr(sanitized.value)
    with pytest.raises(ContinuityAdmissionError, match="test_seams_disabled"):
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(),
            _capability(),
            tensor_factory=lambda value, shape: object(),
        )


def test_post_decode_cancellation_emits_no_extraction_evidence(tmp_path: Path) -> None:
    predecessor_manifest, predecessor_receipt, _, _, _ = _successor_fixture(TaskMode.I2VA)
    payload = b"accepted-video-payload"
    store, committed = _store_with_receipt(tmp_path / "store", predecessor_receipt, payload)
    metadata = VideoAdmissionMetadata(
        width=32,
        height=32,
        declared_frame_count=3,
        duration_ms=1_500,
        frames_per_second=2.0,
        estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
    )
    state = {"cancelled": False}

    def worker(
        value: bytes, limits: ContinuityLimits, cancelled: Callable[[], bool]
    ) -> DecodeResult:
        del value, limits, cancelled
        state["cancelled"] = True
        return DecodeResult(
            tail_bytes=bytes(estimate_tensor_bytes(1, 32, 32)),
            shape=(1, 32, 32, 3),
            decoded_frame_count=3,
        )

    with pytest.raises(ContinuityCancelledError):
        extract_native_tail(
            store,
            predecessor_manifest,
            committed,
            ContinuityLimits(),
            _capability(),
            cancelled=lambda: state["cancelled"],
            metadata_probe=lambda value: metadata,
            worker=cast(DecodeWorker, worker),
            tensor_factory=lambda value, shape: object(),
            test_only_seams=True,
        )


def test_receipt_builder_rejects_wrong_binding_type() -> None:
    predecessor_manifest, predecessor_receipt, plan, _, successor_manifest = _successor_fixture(
        TaskMode.I2VA
    )
    with pytest.raises(ContinuityBindingError, match="native_evidence_type"):
        build_continuity_boundary_receipt(
            build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.I2VA),
            boundary_id="boundary.invalid-binding",
            predecessor_manifest=predecessor_manifest,
            predecessor_receipt=predecessor_receipt,
            successor_plan=plan,
            successor_job=plan.job_for_id("job.segment.next"),
            successor_manifest=successor_manifest,
            binding=cast(Any, object()),
        )


def _sleeping_worker_entry(send_conn: Any, payload: bytes, limits: ContinuityLimits) -> None:
    del limits
    while True:
        time.sleep(0.05)


def test_worker_rejects_unbounded_decode_output_before_pipe_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = VideoAdmissionMetadata(
        width=32,
        height=32,
        declared_frame_count=3,
        duration_ms=1_500,
        frames_per_second=2.0,
        estimated_tensor_bytes=estimate_tensor_bytes(3, 32, 32),
    )

    class CaptureConnection:
        def __init__(self) -> None:
            self.messages: list[object] = []

        def send(self, value: object) -> None:
            self.messages.append(value)

        def close(self) -> None:
            return None

    monkeypatch.setattr(continuity_adapter, "_default_metadata_probe", lambda value: metadata)
    monkeypatch.setattr(
        continuity_adapter,
        "_public_host_decode",
        lambda value: DecodeResult(
            tail_bytes=bytes(estimate_tensor_bytes(1, 32, 32)),
            shape=(1, 32, 32, 3),
            decoded_frame_count=4,
        ),
    )
    connection = CaptureConnection()
    continuity_adapter._decode_worker_entry(connection, b"bounded", ContinuityLimits())
    assert connection.messages == [{"ok": False}]
    assert all(
        type(message) is not dict or "tail_bytes" not in message for message in connection.messages
    )


def test_host_node_source_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "nodes_minimax_h3.py"
    source.write_bytes(b"qualified-source")
    expected = hashlib.sha256(b"qualified-source").hexdigest()
    monkeypatch.setattr(continuity_adapter, "QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256", expected)
    module = SimpleNamespace(__file__=str(source))
    continuity_adapter._assert_qualified_host_node_source(module)

    source.write_bytes(b"modified-source")
    with pytest.raises(ContinuityBindingError, match="host_successor_source_drift"):
        continuity_adapter._assert_qualified_host_node_source(module)


def test_unpinned_m17_08_looking_fixture_is_rejected() -> None:
    _, _, plan, graph, manifest = _successor_fixture(TaskMode.I2VA)
    anchor = graph.resolve_anchors((GraphAnchorRole.FIRST_FRAME,))[0]
    with pytest.raises(ContinuityBindingError, match="accepted_m17_08_identity_mismatch"):
        assert_accepted_m17_08_successor_identity(
            plan,
            plan.job_for_id("job.segment.next"),
            graph,
            anchor,
        )


def test_isolated_worker_timeout_and_cancellation_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(continuity_adapter, "_decode_worker_entry", _sleeping_worker_entry)
    limits = ContinuityLimits(
        decoder_timeout_ms=250,
        cancellation_poll_ms=20,
        termination_grace_ms=100,
        cleanup_deadline_ms=100,
    )
    with pytest.raises(ContinuityTimeoutError):
        continuity_adapter._run_isolated_decode(
            b"bounded", limits, lambda: False, clock=time.monotonic
        )
    started = time.monotonic()

    def cancelled() -> bool:
        return time.monotonic() - started > 0.04

    with pytest.raises(ContinuityCancelledError):
        continuity_adapter._run_isolated_decode(b"bounded", limits, cancelled, clock=time.monotonic)
