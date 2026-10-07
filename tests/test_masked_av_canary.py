"""M20-06 focused tests for the masked AV continuation canary lane.

Offline provables: normal registration never contains the canary; execution refuses before
touching anything unless the lane is enabled, the root is explicitly owned and the
harness-supplied candidate qualification projection parses strictly; the shipped M19-08
rows (mask row unsupported) cannot drive the instrument; hostile receipt strings refuse
typed; and a refused admission fails the node with its classified reason codes.  The
admitted end-to-end path is what the supported-host qualification run exists to prove.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.core.joint_av_latent import (
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import LatentCheckpointBoundary
from comfyui_h3_context.core.masked_av_continuation import MaskedAVContinuationError
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)
from comfyui_h3_context.masked_av_canary import (
    MASKED_AV_CANARY_NODE_ID,
    MASKED_CANARY_MARKER_NAME,
    MASKED_CANARY_MARKER_PAYLOAD,
    H3MaskedAVContinuationCanaryNode,
    MaskedAVCanaryError,
)

ROOT = Path(__file__).resolve().parents[1]

_ENABLE = "H3_CONTEXT_HOST_MASKED_AV_CANARY"
_ROOT_VAR = "H3_CONTEXT_MASKED_AV_CANARY_ROOT"

_REFRESHED_ROWS = [
    {
        "row": "joint_av_latent_descriptor",
        "status": "supported",
        "reason_code": "descriptor_measured_live",
        "consumer": "M20-04",
    },
    {
        "row": "completed_boundary_resume",
        "status": "supported",
        "reason_code": "structural_resume_executes_no_bitexact_oracle",
        "consumer": "M20-05",
    },
    {
        "row": "dual_domain_av_mask",
        "status": "supported",
        "reason_code": "repo_owned_nested_mask_constructor_executes",
        "consumer": "M20-06",
    },
]

_SHIPPED_ROWS = [dict(row) for row in _REFRESHED_ROWS[:2]] + [
    {
        "row": "dual_domain_av_mask",
        "status": "unsupported",
        "reason_code": "no_native_nested_mask_constructor",
        "consumer": "M20-06",
    }
]

_FP_A = "sha256:" + "1a" * 32
_FP_B = "sha256:" + "2b" * 32
_FP_OTHER = "sha256:" + "9f" * 32


def _write_qualification(root: Path, rows: list[dict[str, str]]) -> None:
    payload = {"subject_identity": NATIVE_NODE_SHA256, "rows": rows}
    (root / "accepted_qualification.json").write_text(json.dumps(payload), encoding="utf-8")


def _owned_root(scratch: str, rows: list[dict[str, str]] | None = None) -> Path:
    owned = Path(scratch) / "owned"
    owned.mkdir()
    (owned / MASKED_CANARY_MARKER_NAME).write_text(MASKED_CANARY_MARKER_PAYLOAD, encoding="utf-8")
    if rows is not None:
        _write_qualification(owned, rows)
    return owned


def _receipt_wire() -> str:
    authority = build_joint_av_latent_authority(
        build_temporal_profile(
            AcceptedQualification(
                subject_identity=NATIVE_NODE_SHA256,
                rows=tuple(
                    QualifiedRow(
                        row=entry["row"],
                        status=CapabilityStatus(entry["status"]),
                        reason_code=entry["reason_code"],
                        consumer=entry["consumer"],
                    )
                    for entry in _REFRESHED_ROWS
                ),
            )
        )
    )
    boundary = LatentCheckpointBoundary(
        total_steps=4, completed_steps=2, seed=1, requested_frames=22
    )
    descriptor = build_joint_av_latent_descriptor(
        authority=authority,
        video_shape=(1, 24, 7, 16, 24),
        video_dtype="float32",
        audio_shape=(1, 32, 2, 37),
        audio_dtype="float32",
        requested_frames=22,
        completed_frames=22,
    )
    now = time.time_ns() // 1_000_000
    partial = begin_joint_av_latent_receipt(
        descriptor=descriptor,
        authority=authority,
        artifact_id="masked-cont-001",
        transaction_fingerprint=_FP_B,
        execution_fingerprint=boundary.fingerprint,
        model_fingerprint=_FP_A,
        runtime_fingerprint=_FP_A,
        settings_fingerprint=_FP_A,
        source_id="segment.masked",
        predecessor_artifact_fingerprint=None,
        created_at_ms=now - 1_000,
        expires_at_ms=now + 3_600_000,
    )
    completed = complete_joint_av_latent_receipt(
        partial,
        output_fingerprint=_FP_OTHER,
        byte_length=descriptor.payload_byte_length,
    )
    return completed.to_wire_bytes().decode("utf-8")


def _execute(**overrides: Any) -> dict[str, object]:
    values: dict[str, Any] = {
        "checkpoint_receipt": "",
        "total_steps": 4,
        "completed_steps": 2,
        "seed": 1,
        "requested_frames": 22,
        "video_generate_from": 0,
        "audio_generate_from": 0,
        "model_fingerprint": _FP_A,
        "runtime_fingerprint": _FP_A,
        "settings_fingerprint": _FP_A,
        "source_id": "segment.masked",
    }
    values.update(overrides)
    return H3MaskedAVContinuationCanaryNode().execute(**values)


class RegistrationConditionalityTests(unittest.TestCase):
    def _probe(self, *, enabled: bool) -> subprocess.CompletedProcess[str]:
        root = str(ROOT).replace("\\", "\\\\")
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, "{root}")
            import comfyui_h3_context as package
            print("\\n".join(sorted(package.NODE_CLASS_MAPPINGS)))
            """
        )
        env = {key: value for key, value in os.environ.items() if key != _ENABLE}
        if enabled:
            env[_ENABLE] = "1"
        return subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_normal_registration_excludes_the_canary(self) -> None:
        result = self._probe(enabled=False)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(MASKED_AV_CANARY_NODE_ID, result.stdout)

    def test_env_gated_registration_includes_the_canary(self) -> None:
        result = self._probe(enabled=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(MASKED_AV_CANARY_NODE_ID, result.stdout)

    def test_canary_node_stays_internal_and_experimental(self) -> None:
        node = H3MaskedAVContinuationCanaryNode
        self.assertTrue(node.EXPERIMENTAL)
        self.assertEqual(node.CATEGORY, "h3_context/internal")
        required = node.INPUT_TYPES()["required"]
        for name in (
            "checkpoint_receipt",
            "video_generate_from",
            "audio_generate_from",
            "model_fingerprint",
        ):
            self.assertIn(name, required)


class GatingTests(unittest.TestCase):
    def test_disabled_lane_refuses_before_anything_else(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(_ENABLE, None)
            with self.assertRaisesRegex(MaskedAVCanaryError, "not enabled"):
                _execute()

    def test_root_and_marker_misconfiguration_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = Path(scratch) / "owned"
            owned.mkdir()
            with mock.patch.dict(os.environ, {_ENABLE: "1"}, clear=False):
                os.environ.pop(_ROOT_VAR, None)
                with self.assertRaisesRegex(MaskedAVCanaryError, "not configured"):
                    _execute()
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(MaskedAVCanaryError, "not explicitly owned"):
                    _execute()
            (owned / MASKED_CANARY_MARKER_NAME).write_text("wrong\n", encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(MaskedAVCanaryError, "marker is invalid"):
                    _execute()

    def test_shipped_unsupported_row_cannot_drive_the_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _SHIPPED_ROWS)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(MaskedAVContinuationError, "mask_row_not_supported"):
                    _execute(checkpoint_receipt=_receipt_wire())

    def test_projection_strictness_and_hostile_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(MaskedAVCanaryError, "missing"):
                    _execute()
                (owned / "accepted_qualification.json").write_text(
                    json.dumps({"rows": []}), encoding="utf-8"
                )
                with self.assertRaisesRegex(MaskedAVCanaryError, "malformed"):
                    _execute()
                _write_qualification(owned, _REFRESHED_ROWS)
                with self.assertRaisesRegex(MaskedAVCanaryError, "receipt is invalid"):
                    _execute(checkpoint_receipt="not json")
                with self.assertRaisesRegex(MaskedAVCanaryError, "receipt is oversized"):
                    _execute(checkpoint_receipt="x" * (64 * 1024))


class AdmissionPathTests(unittest.TestCase):
    def test_resume_refusal_fails_the_node_with_reason_codes(self) -> None:
        wire = _receipt_wire()
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _REFRESHED_ROWS)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(
                    MaskedAVCanaryError, "checkpoint resume refused: model_mismatch"
                ):
                    _execute(checkpoint_receipt=wire, model_fingerprint=_FP_OTHER)

    def test_out_of_grid_mask_extent_is_a_typed_refusal(self) -> None:
        wire = _receipt_wire()
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _REFRESHED_ROWS)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(
                    MaskedAVContinuationError, "bounded_integer:generate_from"
                ):
                    _execute(checkpoint_receipt=wire, video_generate_from=8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
