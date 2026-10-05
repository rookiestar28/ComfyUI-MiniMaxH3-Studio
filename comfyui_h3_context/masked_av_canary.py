"""Explicit test-only supported-host canary for dual-domain masked AV continuation.

One qualification instrument, mirroring the accepted latent-resume canary lane: it exists
only when ``H3_CONTEXT_HOST_MASKED_AV_CANARY=1`` and an explicitly owned root is supplied,
it is absent from normal registration, and it runs the REAL M20-04 store, the REAL M20-05
resume admission, the REAL M20-06 masked-continuation admission and the REAL mask-attachment
adapter inside the host process -- nothing is stubbed.

The accepted-qualification projection next to the ownership marker is, for this item, the
CANDIDATE row refresh under test: the M19-08 matrix rows with ``dual_domain_av_mask``
refreshed to ``supported / repo_owned_nested_mask_constructor_executes``.  The qualification
run is exactly the test of that candidate claim -- the canary lane is harness-owned and
invisible to production, so gating the instrument on the claim it exists to measure
fabricates nothing: on PASS the candidate becomes the accepted refresh through the item's
acceptance; on FAIL it is discarded with the run.

Node flow: checkpoint receipt in -> resume admission -> masked-continuation admission
(mask plan on the exact grids, zero-context conserved crop accounting) -> reconstruct the
checkpoint latent through the real codec -> attach the nested dual-domain mask -> LATENT out
plus both decision records.  Any refusal fails the node with its classified reason codes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .adapters.comfyui_joint_av_latent import reconstruct_joint_av_latent
from .adapters.comfyui_masked_av_latent import attach_nested_av_mask
from .adapters.joint_av_latent_store import PrivateJointAVLatentStore
from .core.joint_av_latent import (
    MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
    JointAVLatentError,
    build_joint_av_latent_authority,
    decode_joint_av_latent_receipt,
)
from .core.latent_checkpoint_resume import (
    LatentCheckpointBoundary,
    LatentResumeRequest,
    admit_latent_checkpoint_resume,
    build_latent_resume_authority,
)
from .core.length import MAX_FRAME_COUNT
from .core.masked_av_continuation import (
    admit_masked_av_continuation,
    build_masked_av_authority,
    plan_nested_av_mask,
)
from .core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    CropPlan,
    QualifiedRow,
    TemporalCapabilityProfile,
    build_temporal_profile,
)

MASKED_AV_CANARY_NODE_ID = "comfyui_h3_context.H3Context.MaskedAVContinuationCanary"

#: Root-content contract shared with the orchestrating qualification harness.
MASKED_CANARY_MARKER_NAME = ".h3-context-owned-masked-av-canary"
MASKED_CANARY_MARKER_PAYLOAD = "h3-context-masked-av-canary/1\n"
MASKED_CANARY_QUALIFICATION_FILENAME = "accepted_qualification.json"
_MAX_QUALIFICATION_BYTES = 16 * 1024
_MAX_STEP_WIDGET = 10_000
_MAX_SEED_WIDGET = 2**31 - 1
_MAX_MASK_STEP_WIDGET = 1_000_000


class MaskedAVCanaryError(ValueError):
    """Raised when the canary lane is not explicitly enabled, owned and qualified."""


def _owned_root() -> Path:
    if os.environ.get("H3_CONTEXT_HOST_MASKED_AV_CANARY") != "1":
        raise MaskedAVCanaryError("masked AV canary is not enabled")
    value = os.environ.get("H3_CONTEXT_MASKED_AV_CANARY_ROOT")
    if not value:
        raise MaskedAVCanaryError("masked AV canary root is not configured")
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MaskedAVCanaryError("masked AV canary root is unavailable") from exc
    marker = resolved / MASKED_CANARY_MARKER_NAME
    if root.is_symlink() or not resolved.is_dir() or not marker.is_file():
        raise MaskedAVCanaryError("masked AV canary root is not explicitly owned")
    if marker.read_text(encoding="utf-8") != MASKED_CANARY_MARKER_PAYLOAD:
        raise MaskedAVCanaryError("masked AV canary ownership marker is invalid")
    return resolved


def _load_accepted_profile(root: Path) -> TemporalCapabilityProfile:
    """Parse the harness-supplied candidate qualification projection, strictly."""

    path = root / MASKED_CANARY_QUALIFICATION_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            raise MaskedAVCanaryError("accepted qualification projection is missing")
        if path.stat().st_size > _MAX_QUALIFICATION_BYTES:
            raise MaskedAVCanaryError("accepted qualification projection is oversized")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except MaskedAVCanaryError:
        raise
    except (OSError, ValueError) as exc:
        raise MaskedAVCanaryError("accepted qualification projection is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"subject_identity", "rows"}:
        raise MaskedAVCanaryError("accepted qualification projection is malformed")
    subject = payload["subject_identity"]
    rows = payload["rows"]
    if not isinstance(subject, str) or not isinstance(rows, list) or not rows:
        raise MaskedAVCanaryError("accepted qualification projection is malformed")
    parsed: list[QualifiedRow] = []
    for entry in rows:
        if not isinstance(entry, dict) or set(entry) != {
            "row",
            "status",
            "reason_code",
            "consumer",
        }:
            raise MaskedAVCanaryError("accepted qualification row is malformed")
        if not all(isinstance(entry[key], str) for key in entry):
            raise MaskedAVCanaryError("accepted qualification row is malformed")
        try:
            status = CapabilityStatus(entry["status"])
        except ValueError as exc:
            raise MaskedAVCanaryError("accepted qualification row is malformed") from exc
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
        raise MaskedAVCanaryError("accepted qualification projection is invalid") from exc


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class H3MaskedAVContinuationCanaryNode:
    """Admit one masked continuation through the real gates and emit the masked latent."""

    __h3_context_node_id__ = MASKED_AV_CANARY_NODE_ID
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
                "checkpoint_receipt": ("STRING", {"default": "", "multiline": True}),
                "total_steps": ("INT", {"default": 4, "min": 1, "max": _MAX_STEP_WIDGET}),
                "completed_steps": (
                    "INT",
                    {"default": 2, "min": 1, "max": _MAX_STEP_WIDGET},
                ),
                "seed": ("INT", {"default": 1, "min": 0, "max": _MAX_SEED_WIDGET}),
                "requested_frames": (
                    "INT",
                    {"default": 22, "min": 1, "max": MAX_FRAME_COUNT},
                ),
                "video_generate_from": (
                    "INT",
                    {"default": 0, "min": 0, "max": _MAX_MASK_STEP_WIDGET},
                ),
                "audio_generate_from": (
                    "INT",
                    {"default": 0, "min": 0, "max": _MAX_MASK_STEP_WIDGET},
                ),
                "model_fingerprint": ("STRING", {"default": ""}),
                "runtime_fingerprint": ("STRING", {"default": ""}),
                "settings_fingerprint": ("STRING", {"default": ""}),
                "source_id": ("STRING", {"default": "segment.masked"}),
            }
        }

    def execute(
        self,
        checkpoint_receipt: str,
        total_steps: int,
        completed_steps: int,
        seed: int,
        requested_frames: int,
        video_generate_from: int,
        audio_generate_from: int,
        model_fingerprint: str,
        runtime_fingerprint: str,
        settings_fingerprint: str,
        source_id: str,
    ) -> dict[str, object]:
        root = _owned_root()
        profile = _load_accepted_profile(root)
        descriptor_authority = build_joint_av_latent_authority(profile)
        resume_authority = build_latent_resume_authority(profile)
        masked_authority = build_masked_av_authority(profile)
        encoded = checkpoint_receipt.encode("utf-8")
        if len(encoded) > MAX_JOINT_AV_LATENT_RECEIPT_BYTES:
            raise MaskedAVCanaryError("checkpoint receipt is oversized")
        try:
            receipt = decode_joint_av_latent_receipt(encoded)
        except JointAVLatentError as exc:
            raise MaskedAVCanaryError("checkpoint receipt is invalid") from exc
        boundary = LatentCheckpointBoundary(
            total_steps=total_steps,
            completed_steps=completed_steps,
            seed=seed,
            requested_frames=requested_frames,
        )
        request = LatentResumeRequest(
            descriptor_authority_fingerprint=descriptor_authority.fingerprint,
            boundary=boundary,
            model_fingerprint=model_fingerprint,
            runtime_fingerprint=runtime_fingerprint,
            settings_fingerprint=settings_fingerprint,
            source_id=source_id,
            predecessor_artifact_fingerprint=None,
            now_ms=_now_ms(),
        )
        resume_decision = admit_latent_checkpoint_resume(
            authority=resume_authority,
            descriptor_authority=descriptor_authority,
            request=request,
            receipt=receipt,
        )
        if not resume_decision.admitted:
            raise MaskedAVCanaryError(
                "checkpoint resume refused: " + ",".join(resume_decision.reason_codes)
            )
        mask_plan = plan_nested_av_mask(
            descriptor=receipt.descriptor,
            video_generate_from=video_generate_from,
            audio_generate_from=audio_generate_from,
        )
        crop_plan = CropPlan(
            requested_target_frames=requested_frames,
            leading_context_frames=0,
            trailing_context_frames=0,
            produced_frames=requested_frames,
            leading_crop_frames=0,
            trailing_crop_frames=0,
        )
        decision = admit_masked_av_continuation(
            authority=masked_authority,
            descriptor_authority=descriptor_authority,
            mask_plan=mask_plan,
            resume_decision=resume_decision,
            receipt=receipt,
            crop_plan=crop_plan,
        )
        record: dict[str, Any] = {
            "resume": resume_decision.to_wire(),
            "masked": decision.to_wire(),
        }
        decision_wire = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if not decision.admitted:
            raise MaskedAVCanaryError(
                "masked continuation refused: " + ",".join(decision.reason_codes)
            )
        store = PrivateJointAVLatentStore(root / "store", clock_ms=_now_ms)
        video_bytes, audio_bytes = store.read_domains(receipt)
        latent = reconstruct_joint_av_latent(receipt.descriptor, video_bytes, audio_bytes)
        masked_latent = attach_nested_av_mask(
            latent, mask_plan=mask_plan, descriptor=receipt.descriptor
        )
        return {"ui": {"decision": [decision_wire]}, "result": (masked_latent, decision_wire)}


__all__ = [
    "H3MaskedAVContinuationCanaryNode",
    "MASKED_AV_CANARY_NODE_ID",
    "MASKED_CANARY_MARKER_NAME",
    "MASKED_CANARY_MARKER_PAYLOAD",
    "MASKED_CANARY_QUALIFICATION_FILENAME",
    "MaskedAVCanaryError",
]
