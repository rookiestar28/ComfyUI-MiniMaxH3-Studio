"""Explicit test-only supported-host canaries for exact latent checkpoint resume.

Two qualification instruments, mirroring the accepted H2 media canary: they exist only when
``H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1`` and an explicitly owned root is supplied, they are
absent from normal registration, and they run the REAL M20-04 store, the REAL torch-boundary
codec and the REAL M20-05 admission inside the host process -- nothing is stubbed.  The
accepted qualification projection is a consume-time input placed next to the ownership marker
by the orchestrating harness; the package embeds no acceptance claim of its own.

Save canary: one live joint AV LATENT in, one COMPLETE checkpoint receipt (content-free wire
JSON) out.  Load canary: that receipt plus the declared boundary and identity in, the
reconstructed LATENT plus the admission decision out; any refusal fails the node instead of
emitting a latent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .adapters.comfyui_joint_av_latent import (
    decompose_joint_av_latent,
    reconstruct_joint_av_latent,
)
from .adapters.joint_av_latent_store import PrivateJointAVLatentStore
from .core.joint_av_latent import (
    MAX_JOINT_AV_LATENT_RECEIPT_BYTES,
    JointAVLatentError,
    begin_joint_av_latent_receipt,
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
from .core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    TemporalCapabilityProfile,
    build_temporal_profile,
)

RESUME_SAVE_CANARY_NODE_ID = "comfyui_h3_context.H3Context.LatentResumeCheckpointSaveCanary"
RESUME_LOAD_CANARY_NODE_ID = "comfyui_h3_context.H3Context.LatentResumeCheckpointLoadCanary"
#: Root-content contract shared with the orchestrating qualification harness.
CANARY_MARKER_NAME = ".h3-context-owned-latent-resume-canary"
CANARY_MARKER_PAYLOAD = "h3-context-latent-resume-canary/1\n"
CANARY_QUALIFICATION_FILENAME = "accepted_qualification.json"
_CANARY_MARKER = CANARY_MARKER_NAME
_MARKER_PAYLOAD = CANARY_MARKER_PAYLOAD
_QUALIFICATION_FILE = CANARY_QUALIFICATION_FILENAME
_MAX_QUALIFICATION_BYTES = 16 * 1024
_RECEIPT_TTL_MS = 3_600_000
_MAX_STEP_WIDGET = 10_000
_MAX_SEED_WIDGET = 2**31 - 1
_MAX_FRAMES_WIDGET = MAX_FRAME_COUNT


class LatentResumeCanaryError(ValueError):
    """Raised when the canary lane is not explicitly enabled, owned and qualified."""


def _owned_root() -> Path:
    if os.environ.get("H3_CONTEXT_HOST_LATENT_RESUME_CANARY") != "1":
        raise LatentResumeCanaryError("latent resume canary is not enabled")
    value = os.environ.get("H3_CONTEXT_LATENT_RESUME_CANARY_ROOT")
    if not value:
        raise LatentResumeCanaryError("latent resume canary root is not configured")
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LatentResumeCanaryError("latent resume canary root is unavailable") from exc
    marker = resolved / _CANARY_MARKER
    if root.is_symlink() or not resolved.is_dir() or not marker.is_file():
        raise LatentResumeCanaryError("latent resume canary root is not explicitly owned")
    if marker.read_text(encoding="utf-8") != _MARKER_PAYLOAD:
        raise LatentResumeCanaryError("latent resume canary ownership marker is invalid")
    return resolved


def _load_accepted_profile(root: Path) -> TemporalCapabilityProfile:
    """Parse the harness-supplied accepted qualification projection, strictly."""

    path = root / _QUALIFICATION_FILE
    try:
        if path.is_symlink() or not path.is_file():
            raise LatentResumeCanaryError("accepted qualification projection is missing")
        if path.stat().st_size > _MAX_QUALIFICATION_BYTES:
            raise LatentResumeCanaryError("accepted qualification projection is oversized")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except LatentResumeCanaryError:
        raise
    except (OSError, ValueError) as exc:
        raise LatentResumeCanaryError("accepted qualification projection is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"subject_identity", "rows"}:
        raise LatentResumeCanaryError("accepted qualification projection is malformed")
    subject = payload["subject_identity"]
    rows = payload["rows"]
    if not isinstance(subject, str) or not isinstance(rows, list) or not rows:
        raise LatentResumeCanaryError("accepted qualification projection is malformed")
    parsed: list[QualifiedRow] = []
    for entry in rows:
        if not isinstance(entry, dict) or set(entry) != {
            "row",
            "status",
            "reason_code",
            "consumer",
        }:
            raise LatentResumeCanaryError("accepted qualification row is malformed")
        if not all(isinstance(entry[key], str) for key in entry):
            raise LatentResumeCanaryError("accepted qualification row is malformed")
        try:
            status = CapabilityStatus(entry["status"])
        except ValueError as exc:
            raise LatentResumeCanaryError("accepted qualification row is malformed") from exc
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
        raise LatentResumeCanaryError("accepted qualification projection is invalid") from exc


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _store(root: Path) -> PrivateJointAVLatentStore:
    return PrivateJointAVLatentStore(root / "store", clock_ms=_now_ms)


def _boundary(
    total_steps: int, completed_steps: int, seed: int, requested_frames: int
) -> LatentCheckpointBoundary:
    return LatentCheckpointBoundary(
        total_steps=total_steps,
        completed_steps=completed_steps,
        seed=seed,
        requested_frames=requested_frames,
    )


_IDENTITY_WIDGETS: dict[str, tuple[object, ...]] = {
    "model_fingerprint": ("STRING", {"default": ""}),
    "runtime_fingerprint": ("STRING", {"default": ""}),
    "settings_fingerprint": ("STRING", {"default": ""}),
    "source_id": ("STRING", {"default": "segment.resume"}),
}

_BOUNDARY_WIDGETS: dict[str, tuple[object, ...]] = {
    "total_steps": ("INT", {"default": 4, "min": 1, "max": _MAX_STEP_WIDGET}),
    "completed_steps": ("INT", {"default": 2, "min": 1, "max": _MAX_STEP_WIDGET}),
    "seed": ("INT", {"default": 1, "min": 0, "max": _MAX_SEED_WIDGET}),
    "requested_frames": ("INT", {"default": 22, "min": 1, "max": _MAX_FRAMES_WIDGET}),
}


class H3LatentResumeCheckpointSaveCanaryNode:
    """Persist one live joint AV latent as one declared-boundary checkpoint receipt."""

    __h3_context_node_id__ = RESUME_SAVE_CANARY_NODE_ID
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("checkpoint_receipt",)
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {
            "required": {
                "samples": ("LATENT",),
                "artifact_id": ("STRING", {"default": "resume-checkpoint-001"}),
                "transaction_fingerprint": ("STRING", {"default": ""}),
                **_BOUNDARY_WIDGETS,
                **_IDENTITY_WIDGETS,
            },
            # M20-07: a bridge output artifact declares its left boundary as predecessor;
            # the empty default preserves every earlier workflow byte for byte.
            "optional": {
                "predecessor_artifact_fingerprint": ("STRING", {"default": ""}),
            },
        }

    def execute(
        self,
        samples: dict[str, object],
        artifact_id: str,
        transaction_fingerprint: str,
        total_steps: int,
        completed_steps: int,
        seed: int,
        requested_frames: int,
        model_fingerprint: str,
        runtime_fingerprint: str,
        settings_fingerprint: str,
        source_id: str,
        predecessor_artifact_fingerprint: str = "",
    ) -> dict[str, object]:
        root = _owned_root()
        profile = _load_accepted_profile(root)
        authority = build_joint_av_latent_authority(profile)
        boundary = _boundary(total_steps, completed_steps, seed, requested_frames)
        decomposed = decompose_joint_av_latent(
            samples,
            authority=authority,
            requested_frames=requested_frames,
            completed_frames=requested_frames,
        )
        created = _now_ms()
        partial = begin_joint_av_latent_receipt(
            descriptor=decomposed.descriptor,
            authority=authority,
            artifact_id=artifact_id,
            transaction_fingerprint=transaction_fingerprint,
            execution_fingerprint=boundary.fingerprint,
            model_fingerprint=model_fingerprint,
            runtime_fingerprint=runtime_fingerprint,
            settings_fingerprint=settings_fingerprint,
            source_id=source_id,
            predecessor_artifact_fingerprint=predecessor_artifact_fingerprint or None,
            created_at_ms=created,
            expires_at_ms=created + _RECEIPT_TTL_MS,
        )
        store = _store(root)
        store.begin(partial)
        completed = store.commit(partial, decomposed.payload)
        wire = completed.to_wire_bytes().decode("utf-8")
        # OUTPUT_NODE evidence: the receipt must land in the host history outputs so the
        # loopback harness can read it back without any filesystem side channel.
        return {"ui": {"checkpoint_receipt": [wire]}, "result": (wire,)}


class H3LatentResumeCheckpointLoadCanaryNode:
    """Admit one checkpoint receipt through the real gate and reconstruct its latent."""

    __h3_context_node_id__ = RESUME_LOAD_CANARY_NODE_ID
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
                **_BOUNDARY_WIDGETS,
                **_IDENTITY_WIDGETS,
            }
        }

    def execute(
        self,
        checkpoint_receipt: str,
        total_steps: int,
        completed_steps: int,
        seed: int,
        requested_frames: int,
        model_fingerprint: str,
        runtime_fingerprint: str,
        settings_fingerprint: str,
        source_id: str,
    ) -> dict[str, object]:
        root = _owned_root()
        profile = _load_accepted_profile(root)
        descriptor_authority = build_joint_av_latent_authority(profile)
        resume_authority = build_latent_resume_authority(profile)
        encoded = checkpoint_receipt.encode("utf-8")
        if len(encoded) > MAX_JOINT_AV_LATENT_RECEIPT_BYTES:
            raise LatentResumeCanaryError("checkpoint receipt is oversized")
        try:
            receipt = decode_joint_av_latent_receipt(encoded)
        except JointAVLatentError as exc:
            raise LatentResumeCanaryError("checkpoint receipt is invalid") from exc
        boundary = _boundary(total_steps, completed_steps, seed, requested_frames)
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
        decision = admit_latent_checkpoint_resume(
            authority=resume_authority,
            descriptor_authority=descriptor_authority,
            request=request,
            receipt=receipt,
        )
        decision_wire = json.dumps(decision.to_wire(), sort_keys=True, separators=(",", ":"))
        if not decision.admitted:
            raise LatentResumeCanaryError(
                "checkpoint resume refused: " + ",".join(decision.reason_codes)
            )
        video_bytes, audio_bytes = _store(root).read_domains(receipt)
        latent = reconstruct_joint_av_latent(receipt.descriptor, video_bytes, audio_bytes)
        return {"ui": {"decision": [decision_wire]}, "result": (latent, decision_wire)}


__all__ = [
    "CANARY_MARKER_NAME",
    "CANARY_MARKER_PAYLOAD",
    "CANARY_QUALIFICATION_FILENAME",
    "H3LatentResumeCheckpointLoadCanaryNode",
    "H3LatentResumeCheckpointSaveCanaryNode",
    "LatentResumeCanaryError",
    "RESUME_LOAD_CANARY_NODE_ID",
    "RESUME_SAVE_CANARY_NODE_ID",
]
