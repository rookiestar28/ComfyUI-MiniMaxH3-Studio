"""Run the privacy-safe M17-10 boundary sample on an explicitly supplied ComfyUI host.

The caller supplies the host working directory.  This script never starts, discovers, restarts or
patches ComfyUI, and prints only bounded content-free identities and accounting.
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
HOST_ROOT = Path.cwd()
if not (HOST_ROOT / "comfy_api" / "latest").is_dir():
    raise RuntimeError("supplied_host_root_missing")
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from comfyui_h3_context.adapters.comfyui_continuity import (  # noqa: E402
    _public_host_decode,
    extract_native_tail,
    materialize_public_successor_graph,
)
from comfyui_h3_context.adapters.segment_artifact_store import (  # noqa: E402
    PrivateSegmentArtifactStore,  # noqa: E402
)
from comfyui_h3_context.core import (  # noqa: E402
    QUALIFIED_DECODER_NAME,
    QUALIFIED_DECODER_VERSION,
    QUALIFIED_M17_08_SUCCESSOR_CONNECTION_ORDER,
    QUALIFIED_M17_08_SUCCESSOR_FIXTURE_ID,
    QUALIFIED_M17_08_SUCCESSOR_JOB_ID,
    QUALIFIED_M17_08_SUCCESSOR_NODE_INSTANCE_ID,
    QUALIFIED_M17_08_SUCCESSOR_SOCKET_NAME,
    QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256,
    QUALIFIED_VIDEO_NODE_SOURCE_SHA256,
    QUALIFIED_VIDEO_TYPES_SOURCE_SHA256,
    ContinuityCapability,
    ContinuityLimits,
    ContinuityMode,
    FingerprintDomain,
    GenerationJobSpec,
    GenerationSequencePlan,
    GraphAnchor,
    GraphAnchorRole,
    GraphNode,
    SegmentArtifactReceipt,
    SegmentDeclaration,
    SegmentDuration,
    SegmentRelationKind,
    TaskMode,
    build_continuity_boundary_receipt,
    build_continuity_policy,
    build_generation_sequence_plan,
    create_workspace,
    derive_segment_manifests,
    plan_recompute,
    revise_workspace,
)
from comfyui_h3_context.core.graph_binding import VisibleGraph  # noqa: E402
from comfyui_h3_context.core.segment_artifacts import ArtifactLifecycleState  # noqa: E402
from comfyui_h3_context.core.segment_workspace import (  # noqa: E402
    AcceptedIntentAuthority,
    SegmentContextManifest,
)


def _fp(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode('ascii')).hexdigest()}"


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
        # M17-25. Three frames is not a length the H3 lattice can produce, so the
        # smoke sample now asks for the shortest one that is. The boundary sample
        # does not depend on the value.
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


def _workspace_setup(
    mode: TaskMode,
    output_fingerprint: str,
    byte_length: int,
    *,
    host_node_id: str,
    host_input_ports: tuple[str, ...],
    host_output_ports: tuple[str, ...],
) -> tuple[
    SegmentContextManifest,
    SegmentContextManifest,
    SegmentArtifactReceipt,
    GenerationSequencePlan,
    VisibleGraph,
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
    authorities = tuple(
        AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
        for item in previous_segments
    )
    previous = create_workspace(
        "workspace.m17.10.host",
        previous_segments,
        accepted_intent_authorities=authorities,
    )
    current_segments = (
        previous_segments[0],
        replace(previous_segments[1], producer_settings_fingerprint=_fp("settings.new")),
    )
    current = revise_workspace(
        previous,
        expected_workspace_fingerprint=previous.fingerprint,
        accepted_intent_authorities=tuple(
            AcceptedIntentAuthority(item.segment_id, item.accepted_intent_fingerprint)
            for item in current_segments
        ),
        segments=current_segments,
    )
    manifests = derive_segment_manifests(current)
    predecessor_manifest, successor_manifest = manifests
    predecessor_receipt = SegmentArtifactReceipt(
        artifact_id="artifact.segment.pred.host",
        state=ArtifactLifecycleState.PARTIAL,
        workspace_id=predecessor_manifest.workspace_id,
        workspace_revision=predecessor_manifest.workspace_revision,
        workspace_fingerprint=predecessor_manifest.workspace_fingerprint,
        segment_id=predecessor_manifest.segment_id,
        manifest_fingerprint=predecessor_manifest.fingerprint,
        producer_fingerprint=predecessor_manifest.producer_fingerprint,
        transaction_fingerprint=_fp("transaction.pred.host"),
        graph_fingerprint=_fp("graph.pred.host"),
        native_binding_fingerprint=predecessor_manifest.native_binding_fingerprint,
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        settings_fingerprint=predecessor_manifest.producer_settings_fingerprint,
        source_id=predecessor_manifest.source_id,
        predecessor_artifact_fingerprint=None,
        execution_fingerprint=_fp("execution.pred.host"),
        format_label="video.mp4",
        shape=(3, 32, 32, 3),
        created_at_ms=1_000,
        expires_at_ms=100_000,
        output_fingerprint=None,
        byte_length=0,
    )
    node = GraphNode(
        node_instance_id=QUALIFIED_M17_08_SUCCESSOR_NODE_INSTANCE_ID,
        node_id=host_node_id,
        input_ports=host_input_ports,
        output_ports=host_output_ports,
    )
    graph = VisibleGraph(
        fixture_id=QUALIFIED_M17_08_SUCCESSOR_FIXTURE_ID,
        task_modes=(mode,),
        nodes=(node,),
        anchors=(
            GraphAnchor(
                GraphAnchorRole.FIRST_FRAME,
                node.node_instance_id,
                QUALIFIED_M17_08_SUCCESSOR_SOCKET_NAME,
                QUALIFIED_M17_08_SUCCESSOR_CONNECTION_ORDER,
            ),
        ),
    )
    spec = GenerationJobSpec(
        segment_id=successor_manifest.segment_id,
        job_id=QUALIFIED_M17_08_SUCCESSOR_JOB_ID,
        graph_fingerprint=graph.fingerprint,
        compiled_prompt_fingerprint=_fp("compiled.next.host"),
        model_fingerprint=_fp("model.h3"),
        runtime_fingerprint=_fp("runtime.comfyui.0.32.0"),
        expected_format="video.mp4",
        expected_shape=(3, 32, 32, 3),
        timeout_ms=60_000,
        # M17-20 D6: the smoke lane measures the full output-producing graph,
        # which is the domain App Mode now materializes.
        fingerprint_domain=FingerprintDomain.OUTPUT_PRODUCING_GRAPH,
    )
    recompute = plan_recompute(derive_segment_manifests(previous), manifests)
    plan = build_generation_sequence_plan(
        current,
        manifests,
        recompute,
        (spec,),
        reusable_receipts=(
            replace(
                predecessor_receipt,
                state=ArtifactLifecycleState.COMPLETE,
                output_fingerprint=output_fingerprint,
                byte_length=byte_length,
                receipt_fingerprint=None,
            ),
        ),
    )
    return predecessor_manifest, successor_manifest, predecessor_receipt, plan, graph


def _qualified_host_node_contract() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Materialize the successor node contract from the pinned public host schema."""

    nodes = importlib.import_module("comfy_extras.nodes_minimax_h3")
    schema = nodes.MiniMaxH3ImageToVideo.define_schema()
    input_ports = tuple(item.id for item in schema.inputs if isinstance(item.id, str))
    output_ports = tuple(
        (item.display_name or item.io_type.lower())
        for item in schema.outputs
        if isinstance(item.display_name or item.io_type, str)
    )
    if "first_frame" not in input_ports or "last_frame" not in input_ports:
        raise RuntimeError("host_successor_first_last_inputs_missing")
    return schema.node_id, input_ports, output_ports


def _qualified_host_identity() -> tuple[str, str, str, str, str, str, str]:
    """Observe host provenance and verify the continuity source capabilities."""

    try:
        version_module = importlib.import_module("comfyui_version")
        host_version = str(version_module.__version__)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=HOST_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source_hashes = tuple(
            hashlib.sha256((HOST_ROOT / relative).read_bytes()).hexdigest()
            for relative in (
                "comfy_extras/nodes_minimax_h3.py",
                "comfy_extras/nodes_video.py",
                "comfy_api/latest/_input_impl/video_types.py",
            )
        )
    except Exception:
        raise RuntimeError("qualified_host_identity_unavailable") from None
    node_source_sha256, video_source_sha256, video_types_source_sha256 = source_hashes
    if (
        len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision.lower())
        or node_source_sha256 != QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256
        or video_source_sha256 != QUALIFIED_VIDEO_NODE_SOURCE_SHA256
        or video_types_source_sha256 != QUALIFIED_VIDEO_TYPES_SOURCE_SHA256
    ):
        raise RuntimeError("qualified_host_capability_drift")
    return (
        host_version,
        revision,
        node_source_sha256,
        video_source_sha256,
        video_types_source_sha256,
        QUALIFIED_DECODER_NAME,
        QUALIFIED_DECODER_VERSION,
    )


def _make_video_bytes() -> bytes:
    av = importlib.import_module("av")
    numpy = importlib.import_module("numpy")
    output = io.BytesIO()
    with av.open(output, mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=2)
        stream.width = 32
        stream.height = 32
        stream.pix_fmt = "yuv420p"
        for value in (16, 96, 176):
            array = numpy.full((32, 32, 3), value, dtype=numpy.uint8)
            frame = av.VideoFrame.from_ndarray(array, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def main() -> None:
    av = importlib.import_module("av")
    (
        host_version,
        host_revision,
        minimax_h3_source_sha256,
        video_source_sha256,
        video_types_source_sha256,
        decoder_name,
        decoder_version,
    ) = _qualified_host_identity()
    if str(av.__version__) != decoder_version:
        raise RuntimeError("qualified_decoder_drift")
    payload = _make_video_bytes()
    psutil = importlib.import_module("psutil")
    _public_host_decode(payload)
    gc.collect()
    process = psutil.Process()
    rss_before = int(process.memory_info().rss)
    probe_result = _public_host_decode(payload)
    rss_after = int(process.memory_info().rss)
    gc.collect()
    output_fingerprint = "sha256:" + hashlib.sha256(payload).hexdigest()
    host_node_id, host_input_ports, host_output_ports = _qualified_host_node_contract()
    predecessor_manifest, successor_manifest, partial_receipt, plan, graph = _workspace_setup(
        TaskMode.I2VA,
        output_fingerprint,
        len(payload),
        host_node_id=host_node_id,
        host_input_ports=host_input_ports,
        host_output_ports=host_output_ports,
    )
    with tempfile.TemporaryDirectory(prefix="h3-m17-10-smoke-") as root:
        store = PrivateSegmentArtifactStore(Path(root), clock_ms=lambda: 1_000)
        store.begin(partial_receipt)
        complete_receipt = store.commit(partial_receipt, payload)
        capability = ContinuityCapability(
            host_version=host_version,
            host_revision=host_revision,
            video_node_source_sha256=video_source_sha256,
            video_types_source_sha256=video_types_source_sha256,
            decoder_name=decoder_name,
            decoder_version=decoder_version,
        )
        extraction = extract_native_tail(
            store,
            predecessor_manifest,
            complete_receipt,
            ContinuityLimits(),
            capability,
        )
        job = plan.job_for_id(QUALIFIED_M17_08_SUCCESSOR_JOB_ID)

        from comfyui_h3_context.core import bind_native_tail_to_successor

        binding = bind_native_tail_to_successor(
            extraction,
            successor_plan=plan,
            successor_job=job,
            successor_manifest=successor_manifest,
            visible_graph=graph,
            last_frame_before=None,
            materialize=lambda tail, anchor, successor_job: materialize_public_successor_graph(
                tail,
                anchor,
                successor_job,
                graph,
                None,
                successor_plan=plan,
            ),
        )
        receipt = build_continuity_boundary_receipt(
            build_continuity_policy(ContinuityMode.NATIVE_FRAME_HANDOFF, TaskMode.I2VA),
            boundary_id="boundary.m17.10.host",
            predecessor_manifest=predecessor_manifest,
            predecessor_receipt=complete_receipt,
            successor_plan=plan,
            successor_job=job,
            successor_manifest=successor_manifest,
            binding=binding,
        )
    print(
        json.dumps(
            {
                "status": "PASS",
                "host_version": host_version,
                "host_revision": host_revision,
                "minimax_h3_source_sha256": minimax_h3_source_sha256,
                "decoder_name": decoder_name,
                "decoder_version": decoder_version,
                "video_node_source_sha256": video_source_sha256,
                "video_types_source_sha256": video_types_source_sha256,
                "input_bytes": len(payload),
                "measured_peak_rss_bytes": max(rss_before, rss_after),
                "measured_tail_tensor_bytes": len(probe_result.tail_bytes),
                "declared_frame_count": extraction.admission.declared_frame_count,
                "decoded_frame_count": extraction.decoded_frame_count,
                "carried_frame_count": extraction.carried_frame_count,
                "delivered_frame_count": extraction.delivered_frame_count,
                "successor_job_id": job.job_id,
                "accepted_m17_08_successor_fixture_id": graph.fixture_id,
                "accepted_m17_08_successor_plan_fingerprint": plan.fingerprint,
                "visible_graph_fingerprint": graph.fingerprint,
                "host_graph_materialization": "GraphBuilder_node_mutation_readback",
                "host_successor_node_id": host_node_id,
                "host_first_frame_direction": "input",
                "first_frame_node_instance_id": binding.target_node_instance_id,
                "first_frame_socket_name": binding.target_socket_name,
                "receipt_fingerprint": receipt.fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
