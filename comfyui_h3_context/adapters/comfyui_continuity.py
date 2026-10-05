"""Qualified ComfyUI continuity extraction behind a bounded process boundary.

The adapter accepts bytes only after the caller-owned :class:`PrivateSegmentArtifactStore`
re-inspects an accepted complete receipt.  It never accepts a path or URL, never imports host
modules at package import time, and returns only one process-local IMAGE tail plus content-free
evidence.  The default decoder is an isolated worker so timeout and cancellation cannot leave an
in-process decode running after the caller has received a failure.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import math
import multiprocessing as mp
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from ..core.continuity_handoff import (
    CONTINUITY_IMAGE_CHANNELS,
    CONTINUITY_IMAGE_DTYPE,
    MAX_CONTINUITY_FRAME_COUNT,
    QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256,
    ContinuityAdmissionError,
    ContinuityBindingError,
    ContinuityCancelledError,
    ContinuityCapability,
    ContinuityCleanupError,
    ContinuityError,
    ContinuityLimits,
    ContinuityRuntimeTail,
    ContinuityTimeoutError,
    NativeGraphMaterialization,
    NativeTailExtractionEvidence,
    VideoAdmissionMetadata,
    assert_accepted_m17_08_successor_identity,
    estimate_tensor_bytes,
    validate_predecessor_artifact_admission,
)
from ..core.generation_sequence import GenerationSequenceJob, GenerationSequencePlan
from ..core.graph_binding import GraphAnchor, VisibleGraph
from ..core.segment_artifacts import SegmentArtifactReceipt
from ..core.segment_workspace import SegmentContextManifest
from .segment_artifact_store import (
    ArtifactInspectionStatus,
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)


class ArtifactStoreBoundary(Protocol):
    """The narrow accepted private-store boundary used by the adapter."""

    def inspect(self, expected: SegmentArtifactReceipt) -> object: ...

    def read(self, expected: SegmentArtifactReceipt) -> bytes: ...


class MetadataProbe(Protocol):
    def __call__(self, payload: bytes) -> VideoAdmissionMetadata: ...


class DecodeWorker(Protocol):
    def __call__(
        self,
        payload: bytes,
        limits: ContinuityLimits,
        cancelled: Callable[[], bool],
    ) -> DecodeResult: ...


class TensorFactory(Protocol):
    def __call__(self, payload: bytes, shape: tuple[int, int, int, int]) -> object: ...


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Process-local worker result; it is not a persisted or public contract."""

    tail_bytes: bytes
    shape: tuple[int, int, int, int]
    decoded_frame_count: int

    def __post_init__(self) -> None:
        if type(self.tail_bytes) is not bytes or not self.tail_bytes:
            raise ContinuityAdmissionError("decode_tail_bytes")
        if (
            type(self.shape) is not tuple
            or len(self.shape) != 4
            or self.shape[0] != 1
            or any(type(value) is not int or value <= 0 for value in self.shape)
        ):
            raise ContinuityAdmissionError("decode_tail_shape")
        if self.shape[3] != CONTINUITY_IMAGE_CHANNELS:
            raise ContinuityAdmissionError("decode_tail_channels")
        if (
            type(self.decoded_frame_count) is not int
            or not 1 <= self.decoded_frame_count <= MAX_CONTINUITY_FRAME_COUNT
        ):
            raise ContinuityAdmissionError("decode_frame_count")


@dataclass(frozen=True, slots=True)
class IsolatedDecodeResult:
    """Metadata admission and decoded tail returned by one killable worker."""

    admission: VideoAdmissionMetadata
    result: DecodeResult

    def __post_init__(self) -> None:
        if type(self.admission) is not VideoAdmissionMetadata:
            raise ContinuityAdmissionError("isolated_metadata_type")
        if type(self.result) is not DecodeResult:
            raise ContinuityAdmissionError("isolated_decode_type")


def _public_host_decode(payload: bytes) -> DecodeResult:
    """Decode through the qualified public InputImpl/GetVideoComponents seam only."""

    try:
        latest = importlib.import_module("comfy_api.latest")
        video_nodes = importlib.import_module("comfy_extras.nodes_video")
        input_impl = latest.InputImpl
        get_components = video_nodes.GetVideoComponents
        # IMPORTANT: use the public API and visible node; private _input_impl paths are not a seam.
        video = input_impl.VideoFromFile(io.BytesIO(payload))
        output = get_components.execute(video)
        result = output.result
        if not isinstance(result, tuple) or len(result) < 1:
            raise ContinuityAdmissionError("native_decode_output")
        images = result[0]
        dtype = str(getattr(images, "dtype", ""))
        if dtype not in {"torch.float32", CONTINUITY_IMAGE_DTYPE}:
            raise ContinuityAdmissionError("native_decode_dtype")
        raw_shape = tuple(int(value) for value in tuple(images.shape))
        if len(raw_shape) != 4:
            raise ContinuityAdmissionError("native_decode_shape")
        frame_count, height, width, channels = raw_shape
        if channels != CONTINUITY_IMAGE_CHANNELS:
            raise ContinuityAdmissionError("native_decode_channels")
        tail = images[-1:].detach().cpu().contiguous()
        tail_bytes = cast(Any, tail).numpy().tobytes()
        return DecodeResult(
            tail_bytes=tail_bytes,
            shape=(1, height, width, channels),
            decoded_frame_count=frame_count,
        )
    except ContinuityError:
        raise
    except Exception:
        # Do not copy host, path, prompt or decoder diagnostics into the caller-visible error.
        raise ContinuityAdmissionError("native_decode_failed") from None


def _assert_qualified_host_node_source(module: object) -> None:
    """Fail closed when the public MiniMax node source differs from the qualified host."""

    try:
        source_path = cast(ModuleType, module).__file__
        if type(source_path) is not str or not source_path.lower().endswith(".py"):
            raise ContinuityBindingError("host_successor_source_unavailable")
        observed = hashlib.sha256(Path(source_path).resolve(strict=True).read_bytes()).hexdigest()
    except ContinuityBindingError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ContinuityBindingError("host_successor_source_unavailable") from None
    if observed != QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256:
        raise ContinuityBindingError("host_successor_source_drift")


def materialize_public_successor_graph(
    tail: ContinuityRuntimeTail,
    anchor: GraphAnchor,
    successor_job: GenerationSequenceJob,
    visible_graph: VisibleGraph,
    last_frame_before: object | None,
    *,
    successor_plan: GenerationSequencePlan,
) -> NativeGraphMaterialization:
    """Materialize and read back one real host graph node through its graph builder."""

    if type(tail) is not ContinuityRuntimeTail:
        raise ContinuityBindingError("host_materialization_tail")
    if type(anchor) is not GraphAnchor or type(successor_job) is not GenerationSequenceJob:
        raise ContinuityBindingError("host_materialization_input")
    if type(visible_graph) is not VisibleGraph:
        raise ContinuityBindingError("host_materialization_graph")
    assert_accepted_m17_08_successor_identity(
        successor_plan,
        successor_job,
        visible_graph,
        anchor,
    )
    try:
        host_nodes = importlib.import_module("comfy_extras.nodes_minimax_h3")
    except Exception:
        raise ContinuityBindingError("host_successor_schema_unavailable") from None
    _assert_qualified_host_node_source(host_nodes)
    try:
        schema = host_nodes.MiniMaxH3ImageToVideo.define_schema()
        host_input_ports = tuple(item.id for item in schema.inputs if isinstance(item.id, str))
        graph_utils = importlib.import_module("comfy_execution.graph_utils")
        graph_builder = graph_utils.GraphBuilder(prefix="")
    except Exception:
        raise ContinuityBindingError("host_successor_schema_unavailable") from None
    node = next(
        (
            item
            for item in visible_graph.flatten_nodes()
            if item.node_instance_id == anchor.node_instance_id
        ),
        None,
    )
    if node is None or node.node_id != schema.node_id:
        raise ContinuityBindingError("host_successor_node_mismatch")
    if tuple(node.input_ports) != host_input_ports or anchor.socket_name not in host_input_ports:
        raise ContinuityBindingError("host_successor_input_drift")
    try:
        input_values = {
            item_id: object()
            for item_id in host_input_ports
            if item_id not in {"first_frame", "last_frame"}
        }
        host_node = graph_builder.node(schema.node_id, id=anchor.node_instance_id, **input_values)
        host_node.set_input(anchor.socket_name, tail.tensor)
        bound_input = host_node.get_input(anchor.socket_name)
        if bound_input is not tail.tensor:
            raise ContinuityBindingError("host_successor_first_frame_readback")
        if successor_job.task_mode.value == "fl2va":
            if last_frame_before is None:
                raise ContinuityBindingError("host_successor_last_frame_missing")
            host_node.set_input("last_frame", last_frame_before)
        last_frame_after = host_node.get_input("last_frame")
        graph = graph_builder.finalize()
        serialized = graph.get(anchor.node_instance_id)
        if (
            type(serialized) is not dict
            or serialized.get("class_type") != schema.node_id
            or serialized.get("inputs", {}).get(anchor.socket_name) is not bound_input
        ):
            raise ContinuityBindingError("host_successor_graph_readback")
    except ContinuityError:
        raise
    except Exception:
        raise ContinuityBindingError("host_successor_graph_materialization_failed") from None
    return NativeGraphMaterialization(
        job_id=successor_job.job_id,
        graph_fingerprint=visible_graph.fingerprint,
        node_instance_id=anchor.node_instance_id,
        socket_name=anchor.socket_name,
        connection_order=anchor.connection_order,
        bound_tail=tail,
        bound_input=bound_input,
        last_frame_after=last_frame_after,
    )


def _validate_decoded_result(
    result: DecodeResult,
    admission: VideoAdmissionMetadata,
    limits: ContinuityLimits,
) -> None:
    """Validate worker output before any tail bytes cross the process boundary."""

    if type(result) is not DecodeResult:
        raise ContinuityAdmissionError("native_decode_output")
    if result.decoded_frame_count != admission.declared_frame_count:
        raise ContinuityAdmissionError("metadata_decode_mismatch")
    if result.shape[1] != admission.height or result.shape[2] != admission.width:
        raise ContinuityAdmissionError("metadata_dimension_mismatch")
    limits.validate_decoded(result.decoded_frame_count, result.shape[1], result.shape[2])
    expected_tail_bytes = estimate_tensor_bytes(1, result.shape[1], result.shape[2])
    if len(result.tail_bytes) != expected_tail_bytes:
        raise ContinuityAdmissionError("native_tail_size")
    if result.shape[3] != CONTINUITY_IMAGE_CHANNELS:
        raise ContinuityAdmissionError("native_tail_channels")


def _decode_worker_entry(send_conn: Any, payload: bytes, limits: ContinuityLimits) -> None:
    """Spawn target for metadata admission and public decode in one killable boundary."""

    try:
        admission = _default_metadata_probe(payload)
        limits.validate_admission(admission, len(payload))
        result = _public_host_decode(payload)
        _validate_decoded_result(result, admission, limits)
        send_conn.send(
            {
                "ok": True,
                "metadata": {
                    "width": admission.width,
                    "height": admission.height,
                    "declared_frame_count": admission.declared_frame_count,
                    "duration_ms": admission.duration_ms,
                    "frames_per_second": admission.frames_per_second,
                    "estimated_tensor_bytes": admission.estimated_tensor_bytes,
                },
                "tail_bytes": result.tail_bytes,
                "shape": result.shape,
                "decoded_frame_count": result.decoded_frame_count,
            }
        )
    except BaseException:
        try:
            send_conn.send({"ok": False})
        except Exception as exc:
            del exc
    finally:
        try:
            send_conn.close()
        except Exception as exc:
            del exc


def _decode_result_from_message(message: object) -> DecodeResult:
    if type(message) is not dict or message.get("ok") is not True:
        raise ContinuityAdmissionError("native_decode_failed")
    try:
        return DecodeResult(
            tail_bytes=message["tail_bytes"],
            shape=message["shape"],
            decoded_frame_count=message["decoded_frame_count"],
        )
    except (KeyError, TypeError, ValueError, ContinuityError):
        raise ContinuityAdmissionError("native_decode_output") from None


def _isolated_result_from_message(message: object) -> IsolatedDecodeResult:
    if type(message) is not dict or message.get("ok") is not True:
        raise ContinuityAdmissionError("native_decode_failed")
    try:
        raw_metadata = message["metadata"]
        if type(raw_metadata) is not dict:
            raise TypeError("metadata")
        admission = VideoAdmissionMetadata(
            width=raw_metadata["width"],
            height=raw_metadata["height"],
            declared_frame_count=raw_metadata["declared_frame_count"],
            duration_ms=raw_metadata["duration_ms"],
            frames_per_second=raw_metadata["frames_per_second"],
            estimated_tensor_bytes=raw_metadata["estimated_tensor_bytes"],
        )
        return IsolatedDecodeResult(admission, _decode_result_from_message(message))
    except ContinuityAdmissionError:
        raise
    except (KeyError, TypeError, ValueError, ContinuityError):
        raise ContinuityAdmissionError("native_decode_output") from None


def _terminate_and_join(process: Any, limits: ContinuityLimits) -> None:
    """Terminate a worker and prove it is gone within the declared cleanup envelope."""

    try:
        if process.is_alive():
            process.terminate()
            process.join(limits.termination_grace_ms / 1_000)
        if process.is_alive():
            kill = getattr(process, "kill", None)
            if not callable(kill):
                raise ContinuityCleanupError("worker_termination_unavailable")
            kill()
            process.join(limits.cleanup_deadline_ms / 1_000)
        if process.is_alive():
            raise ContinuityCleanupError("worker_cleanup_deadline")
    except ContinuityCleanupError:
        raise
    except Exception:
        raise ContinuityCleanupError("worker_cleanup_failed") from None


def _run_isolated_decode(
    payload: bytes,
    limits: ContinuityLimits,
    cancelled: Callable[[], bool],
    *,
    clock: Callable[[], float],
) -> IsolatedDecodeResult:
    context = mp.get_context("spawn")
    receive_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(target=_decode_worker_entry, args=(send_conn, payload, limits))
    try:
        process.start()
    except Exception:
        receive_conn.close()
        send_conn.close()
        raise ContinuityAdmissionError("native_worker_unavailable") from None
    send_conn.close()
    deadline = clock() + limits.decoder_timeout_ms / 1_000
    message: object | None = None
    try:
        while message is None:
            try:
                if cancelled():
                    _terminate_and_join(process, limits)
                    raise ContinuityCancelledError("native_decode_cancelled")
            except ContinuityError:
                raise
            except Exception:
                _terminate_and_join(process, limits)
                raise ContinuityCancelledError("native_decode_cancelled") from None
            remaining = deadline - clock()
            if remaining <= 0:
                _terminate_and_join(process, limits)
                raise ContinuityTimeoutError("native_decode_timeout")
            if receive_conn.poll(min(limits.cancellation_poll_ms / 1_000, remaining)):
                try:
                    message = receive_conn.recv()
                except (EOFError, OSError):
                    raise ContinuityAdmissionError("native_decode_failed") from None
            elif not process.is_alive():
                raise ContinuityAdmissionError("native_decode_failed")
        result = _isolated_result_from_message(message)
        if process.is_alive():
            process.join(limits.termination_grace_ms / 1_000)
        if process.is_alive():
            _terminate_and_join(process, limits)
            raise ContinuityCleanupError("worker_did_not_exit")
        return result
    finally:
        try:
            receive_conn.close()
        except Exception as exc:
            del exc
        try:
            if process.is_alive():
                _terminate_and_join(process, limits)
            process.close()
        except ContinuityError:
            raise
        except Exception:
            raise ContinuityCleanupError("worker_cleanup_failed") from None


def _default_metadata_probe(payload: bytes) -> VideoAdmissionMetadata:
    """Read authenticated container metadata without iterating decoded frames."""

    try:
        av = importlib.import_module("av")
        with av.open(io.BytesIO(payload), mode="r") as container:
            streams = tuple(stream for stream in container.streams if stream.type == "video")
            if len(streams) != 1:
                raise ContinuityAdmissionError("video_stream_count")
            stream = streams[0]
            width = int(stream.width)
            height = int(stream.height)
            frame_count = int(stream.frames)
            average_rate = stream.average_rate
            if frame_count <= 0 or average_rate is None:
                raise ContinuityAdmissionError("untrusted_video_metadata")
            frames_per_second = float(average_rate)
            duration_seconds: float | None = None
            container_duration = getattr(container, "duration", None)
            if container_duration is not None:
                duration_seconds = float(container_duration / av.time_base)
            elif stream.duration is not None and stream.time_base is not None:
                duration_seconds = float(stream.duration * stream.time_base)
            if (
                duration_seconds is None
                or not math.isfinite(duration_seconds)
                or duration_seconds <= 0
            ):
                raise ContinuityAdmissionError("untrusted_video_duration")
            duration_ms = max(1, math.ceil(duration_seconds * 1_000))
            return VideoAdmissionMetadata(
                width=width,
                height=height,
                declared_frame_count=frame_count,
                duration_ms=duration_ms,
                frames_per_second=frames_per_second,
                estimated_tensor_bytes=estimate_tensor_bytes(frame_count, height, width),
            )
    except ContinuityError:
        raise
    except Exception:
        raise ContinuityAdmissionError("video_metadata_unavailable") from None


def _default_tensor_factory(payload: bytes, shape: tuple[int, int, int, int]) -> object:
    try:
        torch = importlib.import_module("torch")
        dtype = torch.float32
        return torch.frombuffer(bytearray(payload), dtype=dtype).reshape(shape).clone()
    except Exception:
        raise ContinuityAdmissionError("native_image_runtime_unavailable") from None


def _run_worker(
    payload: bytes,
    limits: ContinuityLimits,
    cancelled: Callable[[], bool],
    worker: DecodeWorker,
    *,
    clock: Callable[[], float],
) -> DecodeResult:
    try:
        if cancelled():
            raise ContinuityCancelledError("native_decode_cancelled")
        result = worker(payload, limits, cancelled)
        if type(result) is not DecodeResult:
            raise ContinuityAdmissionError("native_decode_output")
        return result
    except ContinuityError:
        raise
    except Exception:
        raise ContinuityAdmissionError("native_decode_failed") from None


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    try:
        if cancelled():
            raise ContinuityCancelledError("native_decode_cancelled")
    except ContinuityCancelledError:
        raise
    except Exception:
        raise ContinuityCancelledError("native_decode_cancelled") from None


def _admit_store_payload(
    store: ArtifactStoreBoundary,
    manifest: SegmentContextManifest,
    receipt: SegmentArtifactReceipt,
    limits: ContinuityLimits,
) -> bytes:
    if not isinstance(store, PrivateSegmentArtifactStore):
        raise ContinuityAdmissionError("store_boundary_type")
    try:
        validate_predecessor_artifact_admission(receipt, manifest)
    except ContinuityError:
        raise
    if receipt.byte_length > limits.max_input_bytes:
        raise ContinuityAdmissionError("input_bytes_limit")
    try:
        inspection = store.inspect(receipt)
    except ArtifactStoreError:
        raise ContinuityAdmissionError("artifact_inspection_failed") from None
    if getattr(inspection, "status", None) is not ArtifactInspectionStatus.REUSABLE:
        raise ContinuityAdmissionError("artifact_not_reusable")
    try:
        payload = store.read(receipt)
    except ArtifactStoreError:
        raise ContinuityAdmissionError("artifact_read_failed") from None
    if type(payload) is not bytes or len(payload) != receipt.byte_length:
        raise ContinuityAdmissionError("artifact_length_mismatch")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != receipt.output_fingerprint:
        raise ContinuityAdmissionError("artifact_content_mismatch")
    return payload


def extract_native_tail(
    store: ArtifactStoreBoundary,
    predecessor_manifest: SegmentContextManifest,
    predecessor_receipt: SegmentArtifactReceipt,
    limits: ContinuityLimits,
    capability: ContinuityCapability,
    *,
    cancelled: Callable[[], bool] | None = None,
    metadata_probe: MetadataProbe | None = None,
    worker: DecodeWorker | None = None,
    tensor_factory: TensorFactory | None = None,
    test_only_seams: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> NativeTailExtractionEvidence:
    """Admit, preflight, decode and return actual one-tail extraction evidence."""

    if type(limits) is not ContinuityLimits or type(capability) is not ContinuityCapability:
        raise ContinuityAdmissionError("native_configuration")
    capability.assert_qualified()
    if (
        metadata_probe is not None or worker is not None or tensor_factory is not None
    ) and not test_only_seams:
        raise ContinuityAdmissionError("test_seams_disabled")
    cancel_probe: Callable[[], bool] = (lambda: False) if cancelled is None else cancelled
    payload = _admit_store_payload(store, predecessor_manifest, predecessor_receipt, limits)
    _check_cancelled(cancel_probe)
    if metadata_probe is None and worker is None:
        isolated = _run_isolated_decode(payload, limits, cancel_probe, clock=clock)
        admission = isolated.admission
        result = isolated.result
    else:
        if metadata_probe is None or worker is None:
            raise ContinuityAdmissionError("test_seams_incomplete")
        # Test-only seams retain deterministic unit fixtures; the production path above keeps both
        # metadata pre-admission and public decoding inside one killable worker.
        try:
            admission = (
                metadata_probe(payload)
                if metadata_probe is not None
                else _default_metadata_probe(payload)
            )
        except ContinuityError:
            raise
        except Exception:
            raise ContinuityAdmissionError("video_metadata_unavailable") from None
        if type(admission) is not VideoAdmissionMetadata:
            raise ContinuityAdmissionError("video_metadata_type")
        limits.validate_admission(admission, len(payload))
        result = _run_worker(payload, limits, cancel_probe, worker, clock=clock)
    _check_cancelled(cancel_probe)
    _validate_decoded_result(result, admission, limits)
    make_tensor = _default_tensor_factory if tensor_factory is None else tensor_factory
    try:
        tensor = make_tensor(result.tail_bytes, result.shape)
    except ContinuityError:
        raise
    except Exception:
        raise ContinuityAdmissionError("native_image_runtime_unavailable") from None
    _check_cancelled(cancel_probe)
    tail = ContinuityRuntimeTail(
        tensor=tensor,
        shape=result.shape,
        content_sha256=hashlib.sha256(result.tail_bytes).hexdigest(),
    )
    evidence = NativeTailExtractionEvidence(
        predecessor_receipt_fingerprint=predecessor_receipt.fingerprint,
        predecessor_output_fingerprint=cast(str, predecessor_receipt.output_fingerprint),
        input_byte_length=len(payload),
        admission=admission,
        decoded_frame_count=result.decoded_frame_count,
        carried_frame_count=1,
        delivered_frame_count=1,
        tail=tail,
        capability=capability,
    )
    _check_cancelled(cancel_probe)
    return evidence


__all__ = [
    "ArtifactStoreBoundary",
    "DecodeResult",
    "DecodeWorker",
    "MetadataProbe",
    "TensorFactory",
    "extract_native_tail",
    "materialize_public_successor_graph",
]
