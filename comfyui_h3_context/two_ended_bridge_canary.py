"""Explicit test-only supported-host canary for the two-ended AV bridge (M20-07).

One qualification instrument, mirroring the accepted latent-resume and masked-AV canary
lanes: it exists only when ``H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY=1`` and an explicitly
owned root is supplied, it is absent from normal registration, and it runs the REAL M20-04
store, the REAL M20-07 bridge planning and admission, the REAL byte-exact composition and
the REAL interval-mask attachment inside the host process -- nothing is stubbed.

Node flow: left and right checkpoint receipts in -> bridge plan on the exact grids -> mask
plan derived from it -> bridge admission (authorities, boundary alignment, master-audio
authority, M20-01 crop conservation) -> reconstruct both boundary latents through the real
store and codec -> compose the bridge latent (left tail context, zero middle, right head
context, per domain) -> attach the dual-domain interval mask -> LATENT out plus the full
decision record.  Any refusal fails the node with its classified reason codes, which is the
negative-control surface the qualification harness asserts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .adapters.comfyui_two_ended_bridge import (
    attach_bridge_interval_mask,
    compose_bridge_latent,
)
from .adapters.joint_av_latent_store import PrivateJointAVLatentStore
from .core.joint_av_latent import (
    MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
    JointAVLatentError,
    JointAVLatentReceipt,
    build_joint_av_latent_authority,
    decode_joint_av_latent_receipt,
)
from .core.length import MAX_FRAME_COUNT
from .core.masked_av_continuation import build_masked_av_authority
from .core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    TemporalCapabilityProfile,
    build_temporal_profile,
)
from .core.two_ended_av_bridge import (
    MAX_MASTER_TIMELINE_FRAMES,
    MasterAudioDeclaration,
    admit_two_ended_bridge,
    build_two_ended_bridge_authority,
    plan_bridge_mask,
    plan_two_ended_bridge,
)

TWO_ENDED_BRIDGE_CANARY_NODE_ID = "comfyui_h3_context.H3Context.TwoEndedBridgeCanary"

#: Root-content contract shared with the orchestrating qualification harness.
BRIDGE_CANARY_MARKER_NAME = ".h3-context-owned-two-ended-bridge-canary"
BRIDGE_CANARY_MARKER_PAYLOAD = "h3-context-two-ended-bridge-canary/1\n"
BRIDGE_CANARY_QUALIFICATION_FILENAME = "accepted_qualification.json"
_MAX_QUALIFICATION_BYTES = 16 * 1024
_MAX_TIMELINE_WIDGET = MAX_MASTER_TIMELINE_FRAMES


class TwoEndedBridgeCanaryError(ValueError):
    """Raised when the canary lane is not explicitly enabled, owned and qualified."""


def _owned_root() -> Path:
    if os.environ.get("H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY") != "1":
        raise TwoEndedBridgeCanaryError("two-ended bridge canary is not enabled")
    value = os.environ.get("H3_CONTEXT_TWO_ENDED_BRIDGE_CANARY_ROOT")
    if not value:
        raise TwoEndedBridgeCanaryError("two-ended bridge canary root is not configured")
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TwoEndedBridgeCanaryError("two-ended bridge canary root is unavailable") from exc
    marker = resolved / BRIDGE_CANARY_MARKER_NAME
    if root.is_symlink() or not resolved.is_dir() or not marker.is_file():
        raise TwoEndedBridgeCanaryError("two-ended bridge canary root is not explicitly owned")
    if marker.read_text(encoding="utf-8") != BRIDGE_CANARY_MARKER_PAYLOAD:
        raise TwoEndedBridgeCanaryError("two-ended bridge canary ownership marker is invalid")
    return resolved


def _load_accepted_profile(root: Path) -> TemporalCapabilityProfile:
    """Parse the harness-supplied accepted qualification projection, strictly."""

    path = root / BRIDGE_CANARY_QUALIFICATION_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            raise TwoEndedBridgeCanaryError("accepted qualification projection is missing")
        if path.stat().st_size > _MAX_QUALIFICATION_BYTES:
            raise TwoEndedBridgeCanaryError("accepted qualification projection is oversized")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except TwoEndedBridgeCanaryError:
        raise
    except (OSError, ValueError) as exc:
        raise TwoEndedBridgeCanaryError("accepted qualification projection is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"subject_identity", "rows"}:
        raise TwoEndedBridgeCanaryError("accepted qualification projection is malformed")
    subject = payload["subject_identity"]
    rows = payload["rows"]
    if not isinstance(subject, str) or not isinstance(rows, list) or not rows:
        raise TwoEndedBridgeCanaryError("accepted qualification projection is malformed")
    parsed: list[QualifiedRow] = []
    for entry in rows:
        if not isinstance(entry, dict) or set(entry) != {
            "row",
            "status",
            "reason_code",
            "consumer",
        }:
            raise TwoEndedBridgeCanaryError("accepted qualification row is malformed")
        if not all(isinstance(entry[key], str) for key in entry):
            raise TwoEndedBridgeCanaryError("accepted qualification row is malformed")
        try:
            status = CapabilityStatus(entry["status"])
        except ValueError as exc:
            raise TwoEndedBridgeCanaryError("accepted qualification row is malformed") from exc
        parsed.append(
            QualifiedRow(
                row=entry["row"],
                status=status,
                reason_code=entry["reason_code"],
                consumer=entry["consumer"],
            )
        )
    try:
        accepted = AcceptedQualification(subject_identity=subject, rows=tuple(parsed))
        return build_temporal_profile(accepted)
    except Exception as exc:
        raise TwoEndedBridgeCanaryError("accepted qualification projection is invalid") from exc


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _decode_receipt(wire: str, side: str) -> JointAVLatentReceipt:
    encoded = wire.encode("utf-8")
    if len(encoded) > MAX_JOINT_AV_LATENT_RECEIPT_BYTES:
        raise TwoEndedBridgeCanaryError(f"{side} checkpoint receipt is oversized")
    try:
        return decode_joint_av_latent_receipt(encoded)
    except JointAVLatentError as exc:
        raise TwoEndedBridgeCanaryError(f"{side} checkpoint receipt is invalid") from exc


class H3TwoEndedBridgeCanaryNode:
    """Admit one two-ended bridge through the real gates and emit the masked bridge latent."""

    __h3_context_node_id__ = TWO_ENDED_BRIDGE_CANARY_NODE_ID
    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("samples", "decision")
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {
            "required": {
                "left_receipt": ("STRING", {"default": "", "multiline": True}),
                "right_receipt": ("STRING", {"default": "", "multiline": True}),
                "target_gap_frames": ("INT", {"default": 39, "min": 1, "max": MAX_FRAME_COUNT}),
                "left_context_frames": (
                    "INT",
                    {"default": 51, "min": 1, "max": MAX_FRAME_COUNT},
                ),
                "right_context_frames": (
                    "INT",
                    {"default": 51, "min": 1, "max": MAX_FRAME_COUNT},
                ),
                "left_end_frame": ("INT", {"default": 90, "min": 0, "max": _MAX_TIMELINE_WIDGET}),
                "right_start_frame": (
                    "INT",
                    {"default": 129, "min": 0, "max": _MAX_TIMELINE_WIDGET},
                ),
                "left_lookahead_frames": (
                    "INT",
                    {"default": 0, "min": 0, "max": MAX_FRAME_COUNT},
                ),
                "right_lookahead_frames": (
                    "INT",
                    {"default": 0, "min": 0, "max": MAX_FRAME_COUNT},
                ),
                "master_source_id": ("STRING", {"default": "master.audio"}),
                "coverage_start_frame": (
                    "INT",
                    {"default": 0, "min": 0, "max": _MAX_TIMELINE_WIDGET},
                ),
                "coverage_end_frame": (
                    "INT",
                    {"default": 219, "min": 0, "max": _MAX_TIMELINE_WIDGET},
                ),
            },
            # Divergent per-boundary declarations exist for the negative controls; empty
            # means "declares the master above", which is the admitted path.
            "optional": {
                "left_master_source_id": ("STRING", {"default": ""}),
                "right_master_source_id": ("STRING", {"default": ""}),
            },
        }

    def execute(
        self,
        left_receipt: str,
        right_receipt: str,
        target_gap_frames: int,
        left_context_frames: int,
        right_context_frames: int,
        left_end_frame: int,
        right_start_frame: int,
        left_lookahead_frames: int,
        right_lookahead_frames: int,
        master_source_id: str,
        coverage_start_frame: int,
        coverage_end_frame: int,
        left_master_source_id: str = "",
        right_master_source_id: str = "",
    ) -> dict[str, object]:
        root = _owned_root()
        profile = _load_accepted_profile(root)
        descriptor_authority = build_joint_av_latent_authority(profile)
        mask_authority = build_masked_av_authority(profile)
        bridge_authority = build_two_ended_bridge_authority(profile)
        left = _decode_receipt(left_receipt, "left")
        right = _decode_receipt(right_receipt, "right")
        master = MasterAudioDeclaration(
            master_source_id=master_source_id,
            coverage_start_frame=coverage_start_frame,
            coverage_end_frame=coverage_end_frame,
        )
        plan = plan_two_ended_bridge(
            left_descriptor=left.descriptor,
            right_descriptor=right.descriptor,
            descriptor_authority=descriptor_authority,
            master=master,
            target_gap_frames=target_gap_frames,
            left_context_frames=left_context_frames,
            right_context_frames=right_context_frames,
            left_end_frame=left_end_frame,
            right_start_frame=right_start_frame,
            left_lookahead_frames=left_lookahead_frames,
            right_lookahead_frames=right_lookahead_frames,
        )
        mask_plan = plan_bridge_mask(plan)
        decision = admit_two_ended_bridge(
            authority=bridge_authority,
            descriptor_authority=descriptor_authority,
            mask_authority=mask_authority,
            plan=plan,
            mask_plan=mask_plan,
            left_receipt=left,
            right_receipt=right,
            master=master,
            left_master_source_id=left_master_source_id or master_source_id,
            right_master_source_id=right_master_source_id or master_source_id,
            now_ms=_now_ms(),
        )
        record: dict[str, Any] = {
            "plan": plan.to_wire(),
            "mask_plan": mask_plan.to_wire(),
            "decision": decision.to_wire(),
        }
        decision_wire = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if not decision.admitted:
            raise TwoEndedBridgeCanaryError(
                "two-ended bridge refused: " + ",".join(decision.reason_codes)
            )
        store = PrivateJointAVLatentStore(root / "store", clock_ms=_now_ms)
        left_video, left_audio = store.read_domains(left)
        right_video, right_audio = store.read_domains(right)
        latent = compose_bridge_latent(
            plan=plan,
            left_descriptor=left.descriptor,
            right_descriptor=right.descriptor,
            left_video_bytes=left_video,
            left_audio_bytes=left_audio,
            right_video_bytes=right_video,
            right_audio_bytes=right_audio,
        )
        masked_latent = attach_bridge_interval_mask(
            latent,
            mask_plan=mask_plan,
            plan=plan,
            left_descriptor=left.descriptor,
        )
        return {"ui": {"decision": [decision_wire]}, "result": (masked_latent, decision_wire)}


__all__ = [
    "BRIDGE_CANARY_MARKER_NAME",
    "BRIDGE_CANARY_MARKER_PAYLOAD",
    "BRIDGE_CANARY_QUALIFICATION_FILENAME",
    "H3TwoEndedBridgeCanaryNode",
    "TWO_ENDED_BRIDGE_CANARY_NODE_ID",
    "TwoEndedBridgeCanaryError",
]
