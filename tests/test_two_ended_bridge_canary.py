"""M20-07 focused tests for the two-ended bridge canary lane.

The canary is a qualification instrument: absent from normal registration, present only
under its explicit environment pair, refusing every unowned root, malformed projection and
hostile receipt before any admission work, and failing the node with classified reason
codes when the real admission refuses.  The admitted path's store readback, composition and
attachment run only on the supported host; offline, everything up to and including the
admission decision is exercised with real objects.
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
    JointAVLatentReceipt,
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)
from comfyui_h3_context.core.two_ended_av_bridge import TwoEndedBridgeError
from comfyui_h3_context.two_ended_bridge_canary import (
    BRIDGE_CANARY_MARKER_NAME,
    BRIDGE_CANARY_MARKER_PAYLOAD,
    BRIDGE_CANARY_QUALIFICATION_FILENAME,
    TWO_ENDED_BRIDGE_CANARY_NODE_ID,
    H3TwoEndedBridgeCanaryNode,
    TwoEndedBridgeCanaryError,
)

ROOT = Path(__file__).resolve().parent.parent

_ENABLE = "H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY"
_ROOT_VAR = "H3_CONTEXT_TWO_ENDED_BRIDGE_CANARY_ROOT"

_FP_A = "sha256:" + "1a" * 32
_FP_B = "sha256:" + "2b" * 32
_FP_OTHER = "sha256:" + "9f" * 32


def _rows(bridge_status: str = "supported") -> list[dict[str, str]]:
    return [
        {
            "row": "joint_av_latent_descriptor",
            "status": "supported",
            "reason_code": "descriptor_measured_live",
            "consumer": "M20-04",
        },
        {
            "row": "dual_domain_av_mask",
            "status": "supported",
            "reason_code": "repo_owned_nested_mask_constructor_executes",
            "consumer": "M20-06",
        },
        {
            "row": "two_ended_av_bridge",
            "status": bridge_status,
            "reason_code": (
                "pixel_domain_two_ended_composition_executes"
                if bridge_status == "supported"
                else "not_requalified"
            ),
            "consumer": "M20-07",
        },
    ]


def _write_qualification(owned: Path, rows: list[dict[str, str]]) -> None:
    (owned / BRIDGE_CANARY_QUALIFICATION_FILENAME).write_text(
        json.dumps({"subject_identity": NATIVE_NODE_SHA256, "rows": rows}),
        encoding="utf-8",
    )


def _owned_root(scratch: str, rows: list[dict[str, str]] | None = None) -> Path:
    owned = Path(scratch) / "owned"
    owned.mkdir()
    (owned / BRIDGE_CANARY_MARKER_NAME).write_text(BRIDGE_CANARY_MARKER_PAYLOAD, encoding="utf-8")
    if rows is not None:
        _write_qualification(owned, rows)
    return owned


_PROFILE = build_temporal_profile(
    AcceptedQualification(
        subject_identity=NATIVE_NODE_SHA256,
        rows=tuple(
            QualifiedRow(
                row=entry["row"],
                status=CapabilityStatus(entry["status"]),
                reason_code=entry["reason_code"],
                consumer=entry["consumer"],
            )
            for entry in _rows()
        ),
    )
)
_AUTHORITY = build_joint_av_latent_authority(_PROFILE)


def _receipt_wire(artifact_id: str) -> str:
    descriptor = build_joint_av_latent_descriptor(
        authority=_AUTHORITY,
        video_shape=(1, 24, 27, 1, 1),
        video_dtype="float32",
        audio_shape=(1, 32, 2, 150),
        audio_dtype="float32",
        requested_frames=90,
        completed_frames=90,
    )
    partial = begin_joint_av_latent_receipt(
        descriptor=descriptor,
        authority=_AUTHORITY,
        artifact_id=artifact_id,
        transaction_fingerprint=_FP_B,
        execution_fingerprint=_FP_A,
        model_fingerprint=_FP_A,
        runtime_fingerprint=_FP_A,
        settings_fingerprint=_FP_A,
        source_id="segment.bridge",
        predecessor_artifact_fingerprint=None,
        # The canary admits against real wall-clock time, so the receipt must be live now
        # and still inside the 30-day TTL ceiling.
        created_at_ms=(now_ms := time.time_ns() // 1_000_000),
        expires_at_ms=now_ms + 24 * 60 * 60 * 1_000,
    )
    completed: JointAVLatentReceipt = complete_joint_av_latent_receipt(
        partial,
        output_fingerprint=_FP_OTHER,
        byte_length=partial.descriptor.payload_byte_length,
    )
    return completed.to_wire_bytes().decode("utf-8")


def _execute(**overrides: Any) -> dict[str, object]:
    values: dict[str, Any] = {
        "left_receipt": "",
        "right_receipt": "",
        "target_gap_frames": 39,
        "left_context_frames": 51,
        "right_context_frames": 51,
        "left_end_frame": 90,
        "right_start_frame": 129,
        "left_lookahead_frames": 0,
        "right_lookahead_frames": 0,
        "master_source_id": "master.audio",
        "coverage_start_frame": 0,
        "coverage_end_frame": 240,
    }
    values.update(overrides)
    return H3TwoEndedBridgeCanaryNode().execute(**values)


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
        self.assertNotIn(TWO_ENDED_BRIDGE_CANARY_NODE_ID, result.stdout)

    def test_env_gated_registration_includes_the_canary(self) -> None:
        result = self._probe(enabled=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(TWO_ENDED_BRIDGE_CANARY_NODE_ID, result.stdout)

    def test_canary_node_stays_internal_and_experimental(self) -> None:
        node = H3TwoEndedBridgeCanaryNode
        self.assertTrue(node.EXPERIMENTAL)
        self.assertEqual(node.CATEGORY, "h3_context/internal")
        required = node.INPUT_TYPES()["required"]
        for name in (
            "left_receipt",
            "right_receipt",
            "target_gap_frames",
            "master_source_id",
            "coverage_end_frame",
        ):
            self.assertIn(name, required)
        optional = node.INPUT_TYPES()["optional"]
        self.assertIn("left_master_source_id", optional)
        self.assertIn("right_master_source_id", optional)


class GatingTests(unittest.TestCase):
    def test_disabled_lane_refuses_before_anything_else(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(_ENABLE, None)
            with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "not enabled"):
                _execute()

    def test_root_and_marker_misconfiguration_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = Path(scratch) / "owned"
            owned.mkdir()
            with mock.patch.dict(os.environ, {_ENABLE: "1"}, clear=False):
                os.environ.pop(_ROOT_VAR, None)
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "not configured"):
                    _execute()
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "not explicitly owned"):
                    _execute()
            (owned / BRIDGE_CANARY_MARKER_NAME).write_text("wrong\n", encoding="utf-8")
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "marker is invalid"):
                    _execute()

    def test_unsupported_bridge_row_cannot_drive_the_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _rows("unsupported"))
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(TwoEndedBridgeError, "bridge_row_not_supported"):
                    _execute()

    def test_projection_strictness_and_hostile_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "missing"):
                    _execute()
                (owned / BRIDGE_CANARY_QUALIFICATION_FILENAME).write_text(
                    json.dumps({"rows": []}), encoding="utf-8"
                )
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "malformed"):
                    _execute()
                _write_qualification(owned, _rows())
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "receipt is invalid"):
                    _execute(left_receipt="not json")
                with self.assertRaisesRegex(TwoEndedBridgeCanaryError, "receipt is oversized"):
                    _execute(left_receipt="x" * (64 * 1024))


class AdmissionPathTests(unittest.TestCase):
    def test_master_conflict_fails_the_node_with_reason_codes(self) -> None:
        left = _receipt_wire("bridge-left-001")
        right = _receipt_wire("bridge-right-001")
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _rows())
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(
                    TwoEndedBridgeCanaryError,
                    "two-ended bridge refused: master_audio_conflict:right",
                ):
                    _execute(
                        left_receipt=left,
                        right_receipt=right,
                        right_master_source_id="clip.audio",
                    )

    def test_off_grid_context_is_a_typed_refusal(self) -> None:
        left = _receipt_wire("bridge-left-001")
        right = _receipt_wire("bridge-right-001")
        with tempfile.TemporaryDirectory() as scratch:
            owned = _owned_root(scratch, _rows())
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(TwoEndedBridgeError, "context_extent_off_grid"):
                    _execute(left_receipt=left, right_receipt=right, left_context_frames=50)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
