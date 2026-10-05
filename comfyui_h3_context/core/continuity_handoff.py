"""M17-10 process-local continuity controls and the sole portable boundary receipt.

Policy, limits, extraction evidence, binding evidence and runtime IMAGE values intentionally stay
process-local.  Only :class:`ContinuityBoundaryReceipt` crosses a persistence boundary.  The pure
module never opens media or imports ComfyUI; those actions belong to the qualified adapter.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import cast

from .canonical import canonical_bytes, canonical_fingerprint
from .contracts import TaskMode
from .generation_sequence import GenerationSequenceJob, GenerationSequencePlan
from .graph_binding import GraphAnchor, GraphAnchorRole, VisibleGraph
from .segment_artifacts import ArtifactKind, ArtifactLifecycleState, SegmentArtifactReceipt
from .segment_workspace import SegmentContextManifest

CONTINUITY_BOUNDARY_RECEIPT_SCHEMA = "h3.context.continuity_boundary_receipt.v1"
MAX_CONTINUITY_RECEIPT_BYTES = 32_768
MAX_CONTINUITY_IDENTIFIER_LENGTH = 128
MAX_CONTINUITY_FRAME_COUNT = 1_000_000
MAX_CONTINUITY_IMAGE_DIMENSION = 8_192
MAX_CONTINUITY_RECEIPT_CONNECTION_ORDER = 128
MAX_CONTINUITY_HOST_VERSION_LENGTH = 64
MAX_CONTINUITY_DECODER_VERSION_LENGTH = 64
MAX_CONTINUITY_TENSOR_BYTES = 16 * 1024 * 1024 * 1024

# The public host currently returns float32 RGB IMAGE tensors.  These are part of the qualified
# extraction seam, not configurable wire values; a source/capability drift invalidates the seam.
CONTINUITY_IMAGE_CHANNELS = 3
CONTINUITY_IMAGE_DTYPE = "float32"
CONTINUITY_IMAGE_DTYPE_BYTES = 4

QUALIFIED_COMFYUI_HOST_VERSION = "0.32.0"
QUALIFIED_COMFYUI_HOST_REVISION = (
    "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
)
QUALIFIED_VIDEO_NODE_SOURCE_SHA256 = (
    "727653159de9a6e9d5b37b1101d3ece5df7dce8fc6a5a3a1f27e260afcfa6c14"  # pragma: allowlist secret
)
QUALIFIED_VIDEO_TYPES_SOURCE_SHA256 = (
    "f838d6d3e113c5f885e44fd056c32caa7d9549736663fe669f2b96c39422386b"  # pragma: allowlist secret
)
QUALIFIED_MINIMAX_H3_NODE_SOURCE_SHA256 = (
    "f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064"  # pragma: allowlist secret
)
QUALIFIED_DECODER_NAME = "pyav"
QUALIFIED_DECODER_VERSION = "16.1.0"

# Process-local acceptance fixture pinned from the accepted M17-08 sequence/graph contract.  These
# identities are deliberately not part of ContinuityBoundaryReceipt v1 or any public projection.
QUALIFIED_M17_08_SUCCESSOR_FIXTURE_ID = "fixture.m17.08.accepted.successor.host"
QUALIFIED_M17_08_SUCCESSOR_JOB_ID = "job.m17.08.accepted.successor.host"
QUALIFIED_M17_08_SUCCESSOR_NODE_INSTANCE_ID = "node.m17.08.accepted.successor.host"
QUALIFIED_M17_08_SUCCESSOR_SOCKET_NAME = "first_frame"
QUALIFIED_M17_08_SUCCESSOR_CONNECTION_ORDER = 1
QUALIFIED_M17_08_I2VA_PLAN_FINGERPRINT = (
    "sha256:cec7679b3429e8185f83dbb404007b09361be19033532dfdc889f4e8f7a"
    "99493"  # pragma: allowlist secret
)
QUALIFIED_M17_08_I2VA_GRAPH_FINGERPRINT = (
    "sha256:12e66a4d3e555307f8862c1d47875c3632bfceaf2c21b320c57abd7513748f"
    "09"  # pragma: allowlist secret
)
QUALIFIED_M17_08_FL2VA_PLAN_FINGERPRINT = (
    "sha256:31dd5685215b8b8edcd84ec9c1572a7870bdd19a943b1ac7c2644cb9c34edd"
    "a6"  # pragma: allowlist secret
)
QUALIFIED_M17_08_FL2VA_GRAPH_FINGERPRINT = (
    "sha256:521de2e679b48a1c32fe4597964fc1b138e783fd138ed06af38ed9b0450ce7"
    "c6"  # pragma: allowlist secret
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}\Z")


class ContinuityError(ValueError):
    """Base error for a fail-closed continuity decision or receipt."""


class ContinuityAdmissionError(ContinuityError):
    """Raised before decode when authenticated metadata exceeds a resource limit."""


class ContinuityBindingError(ContinuityError):
    """Raised when the exact accepted M17-08 job/graph/socket cannot be proven."""


class ContinuityTimeoutError(ContinuityError):
    """Raised when a killable decode worker exceeds its wall-clock deadline."""


class ContinuityCancelledError(ContinuityError):
    """Raised when cancellation terminates a decode before a receipt can be emitted."""


class ContinuityCleanupError(ContinuityError):
    """Raised when a timed-out/cancelled worker cannot be terminated and joined."""


class ContinuityReceiptError(ContinuityError):
    """Raised for invalid, non-canonical, unknown or tampered portable receipt data."""


class ContinuityMode(str, Enum):
    CUT = "cut"
    RESTART = "restart"
    NATIVE_FRAME_HANDOFF = "native_frame_handoff"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or len(value) > MAX_CONTINUITY_IDENTIFIER_LENGTH:
        raise ContinuityError(f"bounded_identifier:{field}")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ContinuityError(f"bounded_identifier:{field}")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ContinuityError(f"sha256:{field}")
    return value


def _sha1(value: object, field: str) -> str:
    if type(value) is not str or _SHA1.fullmatch(value) is None:
        raise ContinuityError(f"sha1:{field}")
    return value


def _prefixed_sha256(value: object, field: str) -> str:
    if type(value) is not str or not value.startswith("sha256:"):
        raise ContinuityError(f"sha256_prefix:{field}")
    _sha256(value.removeprefix("sha256:"), field)
    return value


def _receipt_prefixed_sha256(value: object, field: str) -> str:
    if type(value) is not str or not value.startswith("sha256:"):
        raise ContinuityReceiptError(f"{field}_prefix")
    try:
        _sha256(value.removeprefix("sha256:"), field)
    except ContinuityError as exc:
        del exc
        raise ContinuityReceiptError(f"{field}_format") from None
    return value


def _version(
    value: object, field: str, maximum: int = MAX_CONTINUITY_DECODER_VERSION_LENGTH
) -> str:
    if type(value) is not str or len(value) > maximum or _VERSION.fullmatch(value) is None:
        raise ContinuityError(f"version:{field}")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ContinuityError(f"positive_integer:{field}")
    return value


def _nonnegative(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContinuityError(f"bounded_integer:{field}")
    return value


@dataclass(frozen=True, slots=True)
class ContinuityPolicy:
    """Closed process-local mode decision; it has no schema, wire form or fingerprint."""

    mode: ContinuityMode
    successor_task_mode: TaskMode | None = None

    def __post_init__(self) -> None:
        if type(self.mode) is not ContinuityMode:
            raise ContinuityError("policy.mode")
        if self.successor_task_mode is not None and type(self.successor_task_mode) is not TaskMode:
            raise ContinuityError("policy.successor_task_mode")
        if self.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
            if self.successor_task_mode not in {TaskMode.I2VA, TaskMode.FL2VA}:
                raise ContinuityError("native_mode_unsupported_successor")
        elif self.successor_task_mode is None:
            raise ContinuityError("disconnected_mode_successor_missing")

    @property
    def preserves_last_frame(self) -> bool:
        return (
            self.mode is ContinuityMode.NATIVE_FRAME_HANDOFF
            and self.successor_task_mode is TaskMode.FL2VA
        )

    @property
    def successor_socket(self) -> str | None:
        return "first_frame" if self.mode is ContinuityMode.NATIVE_FRAME_HANDOFF else None


def build_continuity_policy(
    mode: ContinuityMode,
    successor_task_mode: TaskMode | None = None,
) -> ContinuityPolicy:
    """Construct one explicit mode; native failure never chooses cut/restart implicitly."""

    return ContinuityPolicy(mode=mode, successor_task_mode=successor_task_mode)


@dataclass(frozen=True, slots=True)
class ContinuityLimits:
    """Process-local pre-admission and worker-safety envelope for one decode attempt."""

    max_input_bytes: int = 64 * 1024 * 1024
    max_width: int = 2_048
    max_height: int = 2_048
    max_decoded_frame_count: int = 512
    max_aggregate_pixels: int = 2_147_483_648
    max_estimated_tensor_bytes: int = 2_147_483_648
    max_duration_ms: int = 30_000
    max_fps: float = 60.0
    decoder_timeout_ms: int = 120_000
    cancellation_poll_ms: int = 50
    termination_grace_ms: int = 1_000
    temporary_resource_quota_bytes: int = 0
    cleanup_deadline_ms: int = 1_000

    def __post_init__(self) -> None:
        for field, value, maximum in (
            ("max_input_bytes", self.max_input_bytes, 2 * 1024 * 1024 * 1024),
            ("max_width", self.max_width, MAX_CONTINUITY_IMAGE_DIMENSION),
            ("max_height", self.max_height, MAX_CONTINUITY_IMAGE_DIMENSION),
            ("max_decoded_frame_count", self.max_decoded_frame_count, MAX_CONTINUITY_FRAME_COUNT),
            ("max_aggregate_pixels", self.max_aggregate_pixels, 8 * 1024 * 1024 * 1024),
            (
                "max_estimated_tensor_bytes",
                self.max_estimated_tensor_bytes,
                MAX_CONTINUITY_TENSOR_BYTES,
            ),
            ("max_duration_ms", self.max_duration_ms, 86_400_000),
            ("decoder_timeout_ms", self.decoder_timeout_ms, 600_000),
            ("cancellation_poll_ms", self.cancellation_poll_ms, 5_000),
            ("termination_grace_ms", self.termination_grace_ms, 10_000),
            (
                "temporary_resource_quota_bytes",
                self.temporary_resource_quota_bytes,
                self.max_input_bytes,
            ),
            ("cleanup_deadline_ms", self.cleanup_deadline_ms, 60_000),
        ):
            if field == "temporary_resource_quota_bytes" and value == 0:
                continue
            _positive(value, f"limits.{field}", maximum)
        if not isinstance(self.max_fps, int | float) or not math.isfinite(self.max_fps):
            raise ContinuityError("limits.max_fps")
        if not 1 <= self.max_fps <= 240:
            raise ContinuityError("limits.max_fps")
        if self.temporary_resource_quota_bytes != 0:
            raise ContinuityError("temporary_resources_not_qualified")

    def validate_admission(self, metadata: VideoAdmissionMetadata, input_bytes: int) -> None:
        if type(metadata) is not VideoAdmissionMetadata:
            raise ContinuityAdmissionError("metadata_type")
        if type(input_bytes) is not int or not 1 <= input_bytes <= self.max_input_bytes:
            raise ContinuityAdmissionError("input_bytes_limit")
        if metadata.width > self.max_width or metadata.height > self.max_height:
            raise ContinuityAdmissionError("dimensions_limit")
        if metadata.declared_frame_count > self.max_decoded_frame_count:
            raise ContinuityAdmissionError("frame_count_limit")
        if (
            metadata.width * metadata.height * metadata.declared_frame_count
            > self.max_aggregate_pixels
        ):
            raise ContinuityAdmissionError("aggregate_pixels_limit")
        if metadata.estimated_tensor_bytes > self.max_estimated_tensor_bytes:
            raise ContinuityAdmissionError("tensor_bytes_limit")
        if metadata.duration_ms > self.max_duration_ms:
            raise ContinuityAdmissionError("duration_limit")
        if metadata.frames_per_second > self.max_fps:
            raise ContinuityAdmissionError("fps_limit")

    def validate_decoded(self, decoded_frame_count: int, height: int, width: int) -> None:
        _positive(decoded_frame_count, "decoded_frame_count", self.max_decoded_frame_count)
        if height > self.max_height or width > self.max_width:
            raise ContinuityAdmissionError("decoded_dimensions_limit")
        if decoded_frame_count * height * width > self.max_aggregate_pixels:
            raise ContinuityAdmissionError("decoded_pixels_limit")
        estimated = estimate_tensor_bytes(decoded_frame_count, height, width)
        if estimated > self.max_estimated_tensor_bytes:
            raise ContinuityAdmissionError("decoded_tensor_bytes_limit")


@dataclass(frozen=True, slots=True)
class VideoAdmissionMetadata:
    """Authenticated container metadata inspected before full IMAGE decode."""

    width: int
    height: int
    declared_frame_count: int
    duration_ms: int
    frames_per_second: float
    estimated_tensor_bytes: int

    def __post_init__(self) -> None:
        _positive(self.width, "metadata.width", MAX_CONTINUITY_IMAGE_DIMENSION)
        _positive(self.height, "metadata.height", MAX_CONTINUITY_IMAGE_DIMENSION)
        _positive(
            self.declared_frame_count,
            "metadata.declared_frame_count",
            MAX_CONTINUITY_FRAME_COUNT,
        )
        _positive(self.duration_ms, "metadata.duration_ms", 86_400_000)
        if not isinstance(self.frames_per_second, int | float) or not math.isfinite(
            self.frames_per_second
        ):
            raise ContinuityError("metadata.fps")
        if not 0 < self.frames_per_second <= 240:
            raise ContinuityError("metadata.fps")
        _positive(
            self.estimated_tensor_bytes,
            "metadata.estimated_tensor_bytes",
            MAX_CONTINUITY_TENSOR_BYTES,
        )


def estimate_tensor_bytes(frame_count: int, height: int, width: int) -> int:
    """Estimate the actual host IMAGE layout: float32, RGB, NHWC."""

    _positive(frame_count, "frame_count", MAX_CONTINUITY_FRAME_COUNT)
    _positive(height, "height", MAX_CONTINUITY_IMAGE_DIMENSION)
    _positive(width, "width", MAX_CONTINUITY_IMAGE_DIMENSION)
    return frame_count * height * width * CONTINUITY_IMAGE_CHANNELS * CONTINUITY_IMAGE_DTYPE_BYTES


@dataclass(frozen=True, slots=True)
class ContinuityCapability:
    """Process-local source/host/decoder identity; no capability fingerprint is persisted."""

    host_version: str
    host_revision: str
    video_node_source_sha256: str
    video_types_source_sha256: str
    decoder_name: str
    decoder_version: str

    def __post_init__(self) -> None:
        _version(self.host_version, "capability.host_version", MAX_CONTINUITY_HOST_VERSION_LENGTH)
        _sha1(self.host_revision, "capability.host_revision")
        _sha256(self.video_node_source_sha256, "capability.video_node_source_sha256")
        _sha256(self.video_types_source_sha256, "capability.video_types_source_sha256")
        _identifier(self.decoder_name, "capability.decoder_name")
        _version(self.decoder_version, "capability.decoder_version")

    def assert_qualified(self) -> None:
        if (
            self.video_node_source_sha256 != QUALIFIED_VIDEO_NODE_SOURCE_SHA256
            or self.video_types_source_sha256 != QUALIFIED_VIDEO_TYPES_SOURCE_SHA256
            or self.decoder_name != QUALIFIED_DECODER_NAME
            or self.decoder_version != QUALIFIED_DECODER_VERSION
        ):
            raise ContinuityAdmissionError("capability_drift")


@dataclass(frozen=True, slots=True)
class ContinuityRuntimeTail:
    """One private runtime IMAGE tail; it has no wire or public projection."""

    tensor: object
    shape: tuple[int, int, int, int]
    content_sha256: str
    dtype: str = CONTINUITY_IMAGE_DTYPE

    def __post_init__(self) -> None:
        if self.tensor is None:
            raise ContinuityError("tail_tensor_missing")
        if type(self.shape) is not tuple or len(self.shape) != 4 or self.shape[0] != 1:
            raise ContinuityError("tail_shape")
        _positive(self.shape[1], "tail.height", MAX_CONTINUITY_IMAGE_DIMENSION)
        _positive(self.shape[2], "tail.width", MAX_CONTINUITY_IMAGE_DIMENSION)
        if self.shape[3] != CONTINUITY_IMAGE_CHANNELS:
            raise ContinuityError("tail_channels")
        if self.dtype != CONTINUITY_IMAGE_DTYPE:
            raise ContinuityError("tail_dtype")
        _sha256(self.content_sha256, "tail.content_sha256")


@dataclass(frozen=True, slots=True)
class NativeTailExtractionEvidence:
    """Actual extraction facts required before native binding may be attempted."""

    predecessor_receipt_fingerprint: str
    predecessor_output_fingerprint: str
    input_byte_length: int
    admission: VideoAdmissionMetadata
    decoded_frame_count: int
    carried_frame_count: int
    delivered_frame_count: int
    tail: ContinuityRuntimeTail
    capability: ContinuityCapability
    audio_not_carried: bool = True

    def __post_init__(self) -> None:
        _prefixed_sha256(self.predecessor_receipt_fingerprint, "extraction.receipt")
        _prefixed_sha256(self.predecessor_output_fingerprint, "extraction.output")
        _positive(self.input_byte_length, "extraction.input_byte_length", 2 * 1024 * 1024 * 1024)
        if type(self.admission) is not VideoAdmissionMetadata:
            raise ContinuityError("extraction.admission")
        _positive(
            self.decoded_frame_count,
            "extraction.decoded_frame_count",
            MAX_CONTINUITY_FRAME_COUNT,
        )
        if self.carried_frame_count != 1 or self.delivered_frame_count != 1:
            raise ContinuityError("extraction_tail_accounting")
        if self.decoded_frame_count < self.carried_frame_count:
            raise ContinuityError("extraction_accounting_order")
        if type(self.tail) is not ContinuityRuntimeTail:
            raise ContinuityError("extraction.tail")
        if type(self.capability) is not ContinuityCapability:
            raise ContinuityError("extraction.capability")
        if self.audio_not_carried is not True:
            raise ContinuityError("audio_must_not_be_carried")


@dataclass(frozen=True, slots=True)
class NativeGraphMaterialization:
    """Process-local result returned by the caller-owned visible graph materializer."""

    job_id: str
    graph_fingerprint: str
    node_instance_id: str
    socket_name: str
    connection_order: int
    bound_tail: ContinuityRuntimeTail
    bound_input: object
    last_frame_after: object | None

    def __post_init__(self) -> None:
        _identifier(self.job_id, "materialization.job_id")
        try:
            _prefixed_sha256(self.graph_fingerprint, "materialization.graph")
        except ContinuityError as exc:
            del exc
            raise ContinuityBindingError("materialization.graph_prefix") from None
        _identifier(self.node_instance_id, "materialization.node_instance_id")
        if self.socket_name != "first_frame":
            raise ContinuityBindingError("materialization.socket")
        _positive(
            self.connection_order,
            "materialization.connection_order",
            MAX_CONTINUITY_RECEIPT_CONNECTION_ORDER,
        )
        if type(self.bound_tail) is not ContinuityRuntimeTail:
            raise ContinuityBindingError("materialization.tail")
        if self.bound_input is not self.bound_tail.tensor:
            raise ContinuityBindingError("materialization.bound_input")


@dataclass(frozen=True, slots=True)
class NativeTailBindingEvidence:
    """Actual materialization result proving one exact first_frame binding."""

    extraction: NativeTailExtractionEvidence
    successor_job_id: str
    successor_segment_id: str
    successor_graph_fingerprint: str
    visible_graph_fingerprint: str
    target_node_instance_id: str
    target_socket_name: str
    target_connection_order: int
    bound_tail: ContinuityRuntimeTail
    last_frame_preserved: bool

    def __post_init__(self) -> None:
        if type(self.extraction) is not NativeTailExtractionEvidence:
            raise ContinuityBindingError("binding.extraction")
        _identifier(self.successor_job_id, "binding.successor_job_id")
        _identifier(self.successor_segment_id, "binding.successor_segment_id")
        for value, field in (
            (self.successor_graph_fingerprint, "binding.successor_graph"),
            (self.visible_graph_fingerprint, "binding.visible_graph"),
        ):
            try:
                _prefixed_sha256(value, field)
            except ContinuityError as exc:
                del exc
                raise ContinuityBindingError(f"{field}_prefix") from None
        _identifier(self.target_node_instance_id, "binding.target_node_instance_id")
        if self.target_socket_name != "first_frame":
            raise ContinuityBindingError("binding.target_socket")
        _positive(
            self.target_connection_order,
            "binding.target_connection_order",
            MAX_CONTINUITY_RECEIPT_CONNECTION_ORDER,
        )
        if self.bound_tail is not self.extraction.tail:
            raise ContinuityBindingError("binding.tail_identity")
        if type(self.last_frame_preserved) is not bool:
            raise ContinuityBindingError("binding.last_frame_preserved")


def _require_input_anchor(visible_graph: VisibleGraph, anchor: GraphAnchor, field: str) -> None:
    """Reject a role anchor that names an output port instead of a successor input."""

    node = next(
        (
            item
            for item in visible_graph.flatten_nodes()
            if item.node_instance_id == anchor.node_instance_id
        ),
        None,
    )
    if node is None or anchor.socket_name not in node.input_ports:
        raise ContinuityBindingError(f"{field}_anchor_not_input")


def assert_accepted_m17_08_successor_identity(
    successor_plan: GenerationSequencePlan,
    successor_job: GenerationSequenceJob,
    visible_graph: VisibleGraph,
    anchor: GraphAnchor,
) -> None:
    """Require the pinned accepted M17-08 successor fixture before host mutation.

    The generic pure binding function remains reusable for ordinary unit fixtures.  The qualified
    host path calls this stricter process-local guard so a newly fabricated M17-08-looking graph
    cannot be mistaken for the accepted successor sample.
    """

    if (
        type(successor_plan) is not GenerationSequencePlan
        or type(successor_job) is not GenerationSequenceJob
        or type(visible_graph) is not VisibleGraph
        or type(anchor) is not GraphAnchor
    ):
        raise ContinuityBindingError("accepted_m17_08_identity_type")
    expected_plan_fingerprint = (
        QUALIFIED_M17_08_I2VA_PLAN_FINGERPRINT
        if successor_job.task_mode is TaskMode.I2VA
        else QUALIFIED_M17_08_FL2VA_PLAN_FINGERPRINT
    )
    expected_graph_fingerprint = (
        QUALIFIED_M17_08_I2VA_GRAPH_FINGERPRINT
        if successor_job.task_mode is TaskMode.I2VA
        else QUALIFIED_M17_08_FL2VA_GRAPH_FINGERPRINT
    )
    try:
        plan_job_matches = successor_plan.job_for_id(successor_job.job_id) == successor_job
    except Exception as exc:
        del exc
        plan_job_matches = False
    if (
        successor_plan.fingerprint != expected_plan_fingerprint
        or successor_plan.workspace_id != "workspace.m17.10.host"
        or not plan_job_matches
        or successor_job.job_id != QUALIFIED_M17_08_SUCCESSOR_JOB_ID
        or successor_job.segment_id != "segment.next"
        or successor_job.graph_fingerprint != expected_graph_fingerprint
        or visible_graph.fixture_id != QUALIFIED_M17_08_SUCCESSOR_FIXTURE_ID
        or visible_graph.fingerprint != expected_graph_fingerprint
        or anchor.node_instance_id != QUALIFIED_M17_08_SUCCESSOR_NODE_INSTANCE_ID
        or anchor.socket_name != QUALIFIED_M17_08_SUCCESSOR_SOCKET_NAME
        or anchor.connection_order != QUALIFIED_M17_08_SUCCESSOR_CONNECTION_ORDER
    ):
        raise ContinuityBindingError("accepted_m17_08_identity_mismatch")


def bind_native_tail_to_successor(
    extraction: NativeTailExtractionEvidence,
    *,
    successor_plan: GenerationSequencePlan,
    successor_job: GenerationSequenceJob,
    successor_manifest: SegmentContextManifest,
    visible_graph: VisibleGraph,
    last_frame_before: object | None,
    materialize: Callable[
        [ContinuityRuntimeTail, GraphAnchor, GenerationSequenceJob], NativeGraphMaterialization
    ],
) -> NativeTailBindingEvidence:
    """Require an explicit caller-owned graph materialization; never synthesize success."""

    if type(extraction) is not NativeTailExtractionEvidence:
        raise ContinuityBindingError("extraction_type")
    if type(successor_plan) is not GenerationSequencePlan:
        raise ContinuityBindingError("successor_plan_type")
    if type(successor_job) is not GenerationSequenceJob:
        raise ContinuityBindingError("successor_job_type")
    if type(successor_manifest) is not SegmentContextManifest:
        raise ContinuityBindingError("successor_manifest_type")
    if type(visible_graph) is not VisibleGraph:
        raise ContinuityBindingError("visible_graph_type")
    if not callable(materialize):
        raise ContinuityBindingError("materializer_missing")
    try:
        accepted_job = successor_plan.job_for_id(successor_job.job_id)
    except Exception as exc:
        del exc
        raise ContinuityBindingError("successor_job_not_in_plan") from None
    if accepted_job != successor_job:
        raise ContinuityBindingError("successor_job_stale")
    if successor_job.manifest_fingerprint != successor_manifest.fingerprint:
        raise ContinuityBindingError("successor_job_manifest")
    if successor_job.segment_id != successor_manifest.segment_id:
        raise ContinuityBindingError("successor_job_segment")
    if successor_job.task_mode not in {TaskMode.I2VA, TaskMode.FL2VA}:
        raise ContinuityBindingError("successor_job_mode")
    if successor_job.predecessor_receipt_fingerprint != extraction.predecessor_receipt_fingerprint:
        raise ContinuityBindingError("successor_job_receipt")
    if successor_job.predecessor_artifact_fingerprint != extraction.predecessor_output_fingerprint:
        raise ContinuityBindingError("successor_job_output")
    try:
        anchor = visible_graph.resolve_anchors((GraphAnchorRole.FIRST_FRAME,))[0]
    except Exception as exc:
        del exc
        raise ContinuityBindingError("first_frame_anchor_missing") from None
    if anchor.socket_name != "first_frame":
        raise ContinuityBindingError("first_frame_anchor_socket")
    _require_input_anchor(visible_graph, anchor, "first_frame")
    if visible_graph.fingerprint != successor_job.graph_fingerprint:
        raise ContinuityBindingError("successor_graph_mismatch")
    if successor_job.task_mode not in visible_graph.task_modes:
        raise ContinuityBindingError("successor_graph_mode")
    if successor_job.task_mode is TaskMode.I2VA:
        if last_frame_before is not None:
            raise ContinuityBindingError("i2va_unexpected_last_frame")
        try:
            visible_graph.resolve_anchors((GraphAnchorRole.LAST_FRAME,))
        except Exception as exc:
            if "missing_anchor:last_frame" not in str(exc):
                del exc
                raise ContinuityBindingError("i2va_conflicting_last_frame_anchor") from None
        else:
            raise ContinuityBindingError("i2va_unexpected_last_frame_anchor")
    else:
        if last_frame_before is None:
            raise ContinuityBindingError("fl2va_last_frame_missing")
        try:
            last_anchor = visible_graph.resolve_anchors((GraphAnchorRole.LAST_FRAME,))[0]
        except Exception as exc:
            del exc
            raise ContinuityBindingError("fl2va_last_frame_anchor_missing") from None
        if last_anchor.socket_name != "last_frame":
            raise ContinuityBindingError("fl2va_last_frame_anchor_socket")
        _require_input_anchor(visible_graph, last_anchor, "last_frame")
        if (
            last_anchor.node_instance_id == anchor.node_instance_id
            and last_anchor.socket_name == anchor.socket_name
        ):
            raise ContinuityBindingError("fl2va_first_last_anchor_alias")
    try:
        result = materialize(extraction.tail, anchor, successor_job)
    except ContinuityError:
        raise
    except Exception as exc:
        del exc
        raise ContinuityBindingError("successor_graph_materialization_failed") from None
    if type(result) is not NativeGraphMaterialization:
        raise ContinuityBindingError("materialization_result_type")
    if result.job_id != successor_job.job_id:
        raise ContinuityBindingError("materialization_job_mismatch")
    if result.graph_fingerprint != visible_graph.fingerprint:
        raise ContinuityBindingError("materialization_graph_mismatch")
    if result.node_instance_id != anchor.node_instance_id:
        raise ContinuityBindingError("materialization_node_mismatch")
    if (
        result.socket_name != anchor.socket_name
        or result.connection_order != anchor.connection_order
    ):
        raise ContinuityBindingError("materialization_anchor_mismatch")
    if result.bound_tail is not extraction.tail:
        raise ContinuityBindingError("materialization_tail_identity")
    if successor_job.task_mode is TaskMode.I2VA:
        if result.last_frame_after is not None:
            raise ContinuityBindingError("i2va_last_frame_mutated")
        last_frame_preserved = False
    else:
        if result.last_frame_after is not last_frame_before:
            raise ContinuityBindingError("fl2va_last_frame_mutated")
        last_frame_preserved = True
    return NativeTailBindingEvidence(
        extraction=extraction,
        successor_job_id=successor_job.job_id,
        successor_segment_id=successor_job.segment_id,
        successor_graph_fingerprint=successor_job.graph_fingerprint,
        visible_graph_fingerprint=visible_graph.fingerprint,
        target_node_instance_id=anchor.node_instance_id,
        target_socket_name=anchor.socket_name,
        target_connection_order=anchor.connection_order,
        bound_tail=result.bound_tail,
        last_frame_preserved=last_frame_preserved,
    )


_RECEIPT_BASE_FIELDS = {
    "schema",
    "boundary_id",
    "continuity_mode",
    "audio_not_carried",
    "receipt_fingerprint",
}
_RECEIPT_NATIVE_FIELDS = {
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
_RECEIPT_RESTART_FIELDS = {"restart_root_id"}


@dataclass(frozen=True, slots=True)
class ContinuityBoundaryReceipt:
    """The sole M17-10 portable contract; native fields are conditionally present, never null."""

    boundary_id: str
    mode: ContinuityMode
    audio_not_carried: bool
    predecessor_segment_id: str | None = None
    predecessor_receipt_fingerprint: str | None = None
    predecessor_output_fingerprint: str | None = None
    successor_segment_id: str | None = None
    successor_job_id: str | None = None
    successor_graph_fingerprint: str | None = None
    first_frame_node_instance_id: str | None = None
    first_frame_socket_name: str | None = None
    first_frame_connection_order: int | None = None
    host_version: str | None = None
    host_revision: str | None = None
    video_node_source_sha256: str | None = None
    video_types_source_sha256: str | None = None
    decoder_name: str | None = None
    decoder_version: str | None = None
    decoded_frame_count: int | None = None
    carried_frame_count: int | None = None
    delivered_frame_count: int | None = None
    tail_content_sha256: str | None = None
    restart_root_id: str | None = None
    receipt_fingerprint: str | None = None
    schema: str = CONTINUITY_BOUNDARY_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONTINUITY_BOUNDARY_RECEIPT_SCHEMA:
            raise ContinuityReceiptError("unsupported_receipt_schema")
        _identifier(self.boundary_id, "receipt.boundary_id")
        if type(self.mode) is not ContinuityMode:
            raise ContinuityReceiptError("receipt.mode")
        if self.audio_not_carried is not True:
            raise ContinuityReceiptError("receipt.audio_not_carried")
        native_values = (
            self.predecessor_segment_id,
            self.predecessor_receipt_fingerprint,
            self.predecessor_output_fingerprint,
            self.successor_segment_id,
            self.successor_job_id,
            self.successor_graph_fingerprint,
            self.first_frame_node_instance_id,
            self.first_frame_socket_name,
            self.first_frame_connection_order,
            self.host_version,
            self.host_revision,
            self.video_node_source_sha256,
            self.video_types_source_sha256,
            self.decoder_name,
            self.decoder_version,
            self.decoded_frame_count,
            self.carried_frame_count,
            self.delivered_frame_count,
            self.tail_content_sha256,
        )
        if self.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
            if any(value is None for value in native_values) or self.restart_root_id is not None:
                raise ContinuityReceiptError("native_receipt_conditional_fields")
            _identifier(cast(str, self.predecessor_segment_id), "receipt.predecessor_segment_id")
            for value, field in (
                (self.predecessor_receipt_fingerprint, "receipt.predecessor_receipt"),
                (self.predecessor_output_fingerprint, "receipt.predecessor_output"),
                (self.successor_graph_fingerprint, "receipt.successor_graph"),
            ):
                _receipt_prefixed_sha256(value, field)
            _sha256(cast(str, self.tail_content_sha256), "receipt.tail_content")
            _identifier(cast(str, self.successor_segment_id), "receipt.successor_segment_id")
            _identifier(cast(str, self.successor_job_id), "receipt.successor_job_id")
            _sha256(
                cast(str, self.successor_graph_fingerprint).removeprefix("sha256:"),
                "receipt.graph",
            )
            _identifier(cast(str, self.first_frame_node_instance_id), "receipt.first_frame_node")
            if self.first_frame_socket_name != "first_frame":
                raise ContinuityReceiptError("receipt.first_frame_socket")
            _positive(
                cast(int, self.first_frame_connection_order),
                "receipt.first_frame_connection_order",
                MAX_CONTINUITY_RECEIPT_CONNECTION_ORDER,
            )
            _version(
                cast(str, self.host_version),
                "receipt.host_version",
                MAX_CONTINUITY_HOST_VERSION_LENGTH,
            )
            _sha1(cast(str, self.host_revision), "receipt.host_revision")
            _sha256(cast(str, self.video_node_source_sha256), "receipt.video_node_source")
            _sha256(cast(str, self.video_types_source_sha256), "receipt.video_types_source")
            _identifier(cast(str, self.decoder_name), "receipt.decoder_name")
            _version(cast(str, self.decoder_version), "receipt.decoder_version")
            _positive(
                cast(int, self.decoded_frame_count),
                "receipt.decoded_frames",
                MAX_CONTINUITY_FRAME_COUNT,
            )
            if self.carried_frame_count != 1 or self.delivered_frame_count != 1:
                raise ContinuityReceiptError("receipt.tail_accounting")
        else:
            if any(value is not None for value in native_values):
                raise ContinuityReceiptError("disconnected_receipt_native_fields")
            if self.mode is ContinuityMode.CUT and self.restart_root_id is not None:
                raise ContinuityReceiptError("cut_restart_root")
            if self.mode is ContinuityMode.RESTART:
                _identifier(self.restart_root_id, "receipt.restart_root_id")
            elif self.restart_root_id is not None:
                raise ContinuityReceiptError("unknown_restart_root")
        expected = canonical_fingerprint(self._wire_without_fingerprint())
        if self.receipt_fingerprint is None:
            object.__setattr__(self, "receipt_fingerprint", expected)
        elif self.receipt_fingerprint != expected:
            raise ContinuityReceiptError("receipt_fingerprint_mismatch")
        if len(canonical_bytes(self.to_wire())) > MAX_CONTINUITY_RECEIPT_BYTES:
            raise ContinuityReceiptError("receipt_wire_limit")

    @property
    def fingerprint(self) -> str:
        if self.receipt_fingerprint is None:  # pragma: no cover - initialized in __post_init__
            raise ContinuityReceiptError("receipt_fingerprint_uninitialized")
        return self.receipt_fingerprint

    def _wire_without_fingerprint(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "boundary_id": self.boundary_id,
            "continuity_mode": self.mode.value,
            "audio_not_carried": self.audio_not_carried,
        }
        if self.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
            value.update(
                {
                    "predecessor_segment_id": self.predecessor_segment_id,
                    "predecessor_receipt_fingerprint": self.predecessor_receipt_fingerprint,
                    "predecessor_output_fingerprint": self.predecessor_output_fingerprint,
                    "successor_segment_id": self.successor_segment_id,
                    "successor_job_id": self.successor_job_id,
                    "successor_graph_fingerprint": self.successor_graph_fingerprint,
                    "first_frame_node_instance_id": self.first_frame_node_instance_id,
                    "first_frame_socket_name": self.first_frame_socket_name,
                    "first_frame_connection_order": self.first_frame_connection_order,
                    "host_version": self.host_version,
                    "host_revision": self.host_revision,
                    "video_node_source_sha256": self.video_node_source_sha256,
                    "video_types_source_sha256": self.video_types_source_sha256,
                    "decoder_name": self.decoder_name,
                    "decoder_version": self.decoder_version,
                    "decoded_frame_count": self.decoded_frame_count,
                    "carried_frame_count": self.carried_frame_count,
                    "delivered_frame_count": self.delivered_frame_count,
                    "tail_content_sha256": self.tail_content_sha256,
                }
            )
        elif self.mode is ContinuityMode.RESTART:
            value["restart_root_id"] = self.restart_root_id
        return value

    def to_wire(self) -> dict[str, object]:
        value = self._wire_without_fingerprint()
        value["receipt_fingerprint"] = self.fingerprint
        return value

    def to_wire_bytes(self) -> bytes:
        return canonical_bytes(self.to_wire())


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContinuityReceiptError("duplicate_receipt_member")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise ContinuityReceiptError("nonfinite_receipt_value")


def decode_continuity_boundary_receipt(
    payload: str | bytes | bytearray,
) -> ContinuityBoundaryReceipt:
    """Strictly decode one canonical receipt and reject unknown/conditional members."""

    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
            text = payload
        except UnicodeError as exc:
            del exc
            raise ContinuityReceiptError("receipt_utf8") from None
    elif type(payload) is bytes:
        encoded = payload
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            del exc
            raise ContinuityReceiptError("receipt_utf8") from None
    elif type(payload) is bytearray:
        encoded = bytes(payload)
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            del exc
            raise ContinuityReceiptError("receipt_utf8") from None
    else:
        raise ContinuityReceiptError("receipt_payload_type")
    if not 1 <= len(encoded) <= MAX_CONTINUITY_RECEIPT_BYTES:
        raise ContinuityReceiptError("receipt_wire_limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except ContinuityReceiptError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        del exc
        raise ContinuityReceiptError("invalid_receipt_json") from None
    if type(value) is not dict:
        raise ContinuityReceiptError("receipt_wire_type")
    if value.get("schema") != CONTINUITY_BOUNDARY_RECEIPT_SCHEMA:
        raise ContinuityReceiptError("unsupported_receipt_schema")
    try:
        mode = ContinuityMode(value["continuity_mode"])
    except (KeyError, ValueError, TypeError) as exc:
        del exc
        raise ContinuityReceiptError("receipt_mode") from None
    expected = (
        _RECEIPT_BASE_FIELDS
        | (_RECEIPT_NATIVE_FIELDS if mode is ContinuityMode.NATIVE_FRAME_HANDOFF else set())
        | (_RECEIPT_RESTART_FIELDS if mode is ContinuityMode.RESTART else set())
    )
    if set(value) != expected:
        raise ContinuityReceiptError("receipt_wire_members")
    try:
        if canonical_bytes(value) != encoded:
            raise ContinuityReceiptError("receipt_noncanonical")
        return ContinuityBoundaryReceipt(
            schema=value["schema"],
            boundary_id=value["boundary_id"],
            mode=mode,
            audio_not_carried=value["audio_not_carried"],
            predecessor_segment_id=value.get("predecessor_segment_id"),
            predecessor_receipt_fingerprint=value.get("predecessor_receipt_fingerprint"),
            predecessor_output_fingerprint=value.get("predecessor_output_fingerprint"),
            successor_segment_id=value.get("successor_segment_id"),
            successor_job_id=value.get("successor_job_id"),
            successor_graph_fingerprint=value.get("successor_graph_fingerprint"),
            first_frame_node_instance_id=value.get("first_frame_node_instance_id"),
            first_frame_socket_name=value.get("first_frame_socket_name"),
            first_frame_connection_order=value.get("first_frame_connection_order"),
            host_version=value.get("host_version"),
            host_revision=value.get("host_revision"),
            video_node_source_sha256=value.get("video_node_source_sha256"),
            video_types_source_sha256=value.get("video_types_source_sha256"),
            decoder_name=value.get("decoder_name"),
            decoder_version=value.get("decoder_version"),
            decoded_frame_count=value.get("decoded_frame_count"),
            carried_frame_count=value.get("carried_frame_count"),
            delivered_frame_count=value.get("delivered_frame_count"),
            tail_content_sha256=value.get("tail_content_sha256"),
            restart_root_id=value.get("restart_root_id"),
            receipt_fingerprint=value["receipt_fingerprint"],
        )
    except ContinuityReceiptError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        del exc
        raise ContinuityReceiptError("receipt_field_validation") from None


def _verify_manifest(manifest: SegmentContextManifest, field: str) -> None:
    if type(manifest) is not SegmentContextManifest:
        raise ContinuityBindingError(f"{field}_type")
    if manifest.fingerprint != canonical_fingerprint(manifest._wire_without_fingerprint()):
        raise ContinuityBindingError(f"{field}_tampered")


def _verify_predecessor_receipt(
    receipt: SegmentArtifactReceipt,
    manifest: SegmentContextManifest,
) -> None:
    if type(receipt) is not SegmentArtifactReceipt:
        raise ContinuityBindingError("predecessor_receipt_type")
    if receipt.fingerprint != canonical_fingerprint(receipt._wire_without_fingerprint()):
        raise ContinuityBindingError("predecessor_receipt_tampered")
    if receipt.artifact_kind is not ArtifactKind.SEGMENT_OUTPUT:
        raise ContinuityBindingError("predecessor_wrong_kind")
    if receipt.state is not ArtifactLifecycleState.COMPLETE or receipt.output_fingerprint is None:
        raise ContinuityBindingError("predecessor_not_complete")
    if not (
        receipt.workspace_id == manifest.workspace_id
        and receipt.workspace_revision == manifest.workspace_revision
        and receipt.workspace_fingerprint == manifest.workspace_fingerprint
        and receipt.segment_id == manifest.segment_id
        and receipt.manifest_fingerprint == manifest.fingerprint
        and receipt.producer_fingerprint == manifest.producer_fingerprint
        and receipt.native_binding_fingerprint == manifest.native_binding_fingerprint
        and receipt.settings_fingerprint == manifest.producer_settings_fingerprint
        and receipt.source_id == manifest.source_id
    ):
        raise ContinuityBindingError("predecessor_identity_mismatch")


def validate_predecessor_artifact_admission(
    receipt: SegmentArtifactReceipt,
    manifest: SegmentContextManifest,
) -> None:
    """Validate the complete accepted-store identity before any media bytes are decoded."""

    _verify_manifest(manifest, "predecessor_manifest")
    _verify_predecessor_receipt(receipt, manifest)


def build_continuity_boundary_receipt(
    policy: ContinuityPolicy,
    *,
    boundary_id: str,
    predecessor_manifest: SegmentContextManifest | None = None,
    predecessor_receipt: SegmentArtifactReceipt | None = None,
    successor_plan: GenerationSequencePlan | None = None,
    successor_job: GenerationSequenceJob | None = None,
    successor_manifest: SegmentContextManifest | None = None,
    binding: NativeTailBindingEvidence | None = None,
    restart_root_id: str | None = None,
) -> ContinuityBoundaryReceipt:
    """Construct a receipt only from actual evidence, or an explicit cut/restart decision."""

    if type(policy) is not ContinuityPolicy:
        raise ContinuityReceiptError("policy_type")
    _identifier(boundary_id, "boundary_id")
    if policy.mode is ContinuityMode.NATIVE_FRAME_HANDOFF:
        required = (
            predecessor_manifest,
            predecessor_receipt,
            successor_plan,
            successor_job,
            successor_manifest,
            binding,
        )
        if any(value is None for value in required):
            raise ContinuityReceiptError("native_evidence_missing")
        if (
            type(predecessor_manifest) is not SegmentContextManifest
            or type(predecessor_receipt) is not SegmentArtifactReceipt
            or type(successor_plan) is not GenerationSequencePlan
            or type(successor_job) is not GenerationSequenceJob
            or type(successor_manifest) is not SegmentContextManifest
            or type(binding) is not NativeTailBindingEvidence
        ):
            raise ContinuityBindingError("native_evidence_type")
        _verify_manifest(predecessor_manifest, "predecessor_manifest")
        _verify_manifest(successor_manifest, "successor_manifest")
        _verify_predecessor_receipt(predecessor_receipt, predecessor_manifest)
        if successor_job.task_mode is not policy.successor_task_mode:
            raise ContinuityBindingError("policy_successor_mode")
        try:
            accepted_successor_job = successor_plan.job_for_id(successor_job.job_id)
        except Exception as exc:
            del exc
            raise ContinuityBindingError("successor_job_not_accepted") from None
        if successor_job != accepted_successor_job:
            raise ContinuityBindingError("successor_job_not_accepted")
        if successor_job.segment_id != successor_manifest.segment_id:
            raise ContinuityBindingError("successor_segment_mismatch")
        if successor_job.manifest_fingerprint != successor_manifest.fingerprint:
            raise ContinuityBindingError("successor_manifest_mismatch")
        if binding.extraction.predecessor_receipt_fingerprint != predecessor_receipt.fingerprint:
            raise ContinuityBindingError("binding_receipt_mismatch")
        if (
            binding.extraction.predecessor_output_fingerprint
            != predecessor_receipt.output_fingerprint
        ):
            raise ContinuityBindingError("binding_output_mismatch")
        if binding.successor_job_id != successor_job.job_id:
            raise ContinuityBindingError("binding_job_mismatch")
        if binding.successor_segment_id != successor_job.segment_id:
            raise ContinuityBindingError("binding_segment_mismatch")
        if binding.successor_graph_fingerprint != successor_job.graph_fingerprint:
            raise ContinuityBindingError("binding_graph_mismatch")
        if binding.visible_graph_fingerprint != successor_job.graph_fingerprint:
            raise ContinuityBindingError("binding_visible_graph_mismatch")
        if binding.last_frame_preserved != (successor_job.task_mode is TaskMode.FL2VA):
            raise ContinuityBindingError("binding_last_frame_policy")
        capability = binding.extraction.capability
        capability.assert_qualified()
        return ContinuityBoundaryReceipt(
            boundary_id=boundary_id,
            mode=policy.mode,
            audio_not_carried=True,
            predecessor_segment_id=predecessor_manifest.segment_id,
            predecessor_receipt_fingerprint=predecessor_receipt.fingerprint,
            predecessor_output_fingerprint=predecessor_receipt.output_fingerprint,
            successor_segment_id=successor_job.segment_id,
            successor_job_id=successor_job.job_id,
            successor_graph_fingerprint=binding.visible_graph_fingerprint,
            first_frame_node_instance_id=binding.target_node_instance_id,
            first_frame_socket_name=binding.target_socket_name,
            first_frame_connection_order=binding.target_connection_order,
            host_version=capability.host_version,
            host_revision=capability.host_revision,
            video_node_source_sha256=capability.video_node_source_sha256,
            video_types_source_sha256=capability.video_types_source_sha256,
            decoder_name=capability.decoder_name,
            decoder_version=capability.decoder_version,
            decoded_frame_count=binding.extraction.decoded_frame_count,
            carried_frame_count=binding.extraction.carried_frame_count,
            delivered_frame_count=binding.extraction.delivered_frame_count,
            tail_content_sha256=binding.extraction.tail.content_sha256,
        )
    if any(
        value is not None
        for value in (
            predecessor_manifest,
            predecessor_receipt,
            successor_plan,
            successor_job,
            successor_manifest,
            binding,
        )
    ):
        raise ContinuityReceiptError("disconnected_native_evidence")
    if policy.mode is ContinuityMode.CUT and restart_root_id is not None:
        raise ContinuityReceiptError("cut_restart_root")
    if policy.mode is ContinuityMode.RESTART and restart_root_id is None:
        raise ContinuityReceiptError("restart_root_missing")
    return ContinuityBoundaryReceipt(
        boundary_id=boundary_id,
        mode=policy.mode,
        audio_not_carried=True,
        restart_root_id=restart_root_id,
    )


__all__ = [
    "CONTINUITY_BOUNDARY_RECEIPT_SCHEMA",
    "CONTINUITY_IMAGE_CHANNELS",
    "CONTINUITY_IMAGE_DTYPE",
    "CONTINUITY_IMAGE_DTYPE_BYTES",
    "MAX_CONTINUITY_RECEIPT_BYTES",
    "ContinuityAdmissionError",
    "ContinuityBindingError",
    "ContinuityBoundaryReceipt",
    "ContinuityCancelledError",
    "ContinuityCapability",
    "ContinuityCleanupError",
    "ContinuityError",
    "ContinuityLimits",
    "ContinuityMode",
    "ContinuityPolicy",
    "ContinuityReceiptError",
    "ContinuityRuntimeTail",
    "ContinuityTimeoutError",
    "NativeGraphMaterialization",
    "NativeTailBindingEvidence",
    "NativeTailExtractionEvidence",
    "QUALIFIED_M17_08_FL2VA_GRAPH_FINGERPRINT",
    "QUALIFIED_M17_08_FL2VA_PLAN_FINGERPRINT",
    "QUALIFIED_M17_08_I2VA_GRAPH_FINGERPRINT",
    "QUALIFIED_M17_08_I2VA_PLAN_FINGERPRINT",
    "QUALIFIED_M17_08_SUCCESSOR_CONNECTION_ORDER",
    "QUALIFIED_M17_08_SUCCESSOR_FIXTURE_ID",
    "QUALIFIED_M17_08_SUCCESSOR_JOB_ID",
    "QUALIFIED_M17_08_SUCCESSOR_NODE_INSTANCE_ID",
    "QUALIFIED_M17_08_SUCCESSOR_SOCKET_NAME",
    "QUALIFIED_COMFYUI_HOST_REVISION",
    "QUALIFIED_COMFYUI_HOST_VERSION",
    "QUALIFIED_DECODER_NAME",
    "QUALIFIED_DECODER_VERSION",
    "QUALIFIED_VIDEO_NODE_SOURCE_SHA256",
    "QUALIFIED_VIDEO_TYPES_SOURCE_SHA256",
    "VideoAdmissionMetadata",
    "assert_accepted_m17_08_successor_identity",
    "bind_native_tail_to_successor",
    "build_continuity_boundary_receipt",
    "build_continuity_policy",
    "decode_continuity_boundary_receipt",
    "estimate_tensor_bytes",
    "validate_predecessor_artifact_admission",
]
