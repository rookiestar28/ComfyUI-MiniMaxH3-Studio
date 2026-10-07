"""M20-05 focused tests for the latent resume canary lane.

The canaries are qualification instruments, so what is provable offline is the fail-closed
order: normal registration never contains them; execution refuses before touching anything
unless the lane is enabled, the root is explicitly owned, and the harness-supplied accepted
qualification projection parses strictly; and only then does the save path reach the torch
boundary or the load path reach the real admission gate.  The admitted end-to-end path is
exactly what the supported-host qualification run exists to prove and is not simulated here.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from official_temporal_parity import NATIVE_NODE_SHA256

from comfyui_h3_context.adapters.comfyui_joint_av_latent import (
    JointAVLatentAdapterError,
)
from comfyui_h3_context.core.joint_av_latent import (
    begin_joint_av_latent_receipt,
    build_joint_av_latent_authority,
    build_joint_av_latent_descriptor,
    complete_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import LatentCheckpointBoundary
from comfyui_h3_context.core.temporal_profile import (
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    build_temporal_profile,
)
from comfyui_h3_context.latent_resume_canary import (
    RESUME_LOAD_CANARY_NODE_ID,
    RESUME_SAVE_CANARY_NODE_ID,
    H3LatentResumeCheckpointLoadCanaryNode,
    H3LatentResumeCheckpointSaveCanaryNode,
    LatentResumeCanaryError,
)

ROOT = Path(__file__).resolve().parents[1]

_ENABLE = "H3_CONTEXT_HOST_LATENT_RESUME_CANARY"
_ROOT_VAR = "H3_CONTEXT_LATENT_RESUME_CANARY_ROOT"
_MARKER = ".h3-context-owned-latent-resume-canary"
_MARKER_PAYLOAD = "h3-context-latent-resume-canary/1\n"

_ROWS = [
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
]

_FP_A = "sha256:" + "1a" * 32
_FP_B = "sha256:" + "2b" * 32
_FP_OTHER = "sha256:" + "9f" * 32


def _identity_widgets(model_fingerprint: str = _FP_A) -> dict[str, Any]:
    return {
        "model_fingerprint": model_fingerprint,
        "runtime_fingerprint": _FP_A,
        "settings_fingerprint": _FP_A,
        "source_id": "segment.resume",
    }


def _boundary_widgets() -> dict[str, Any]:
    return {"total_steps": 4, "completed_steps": 2, "seed": 1, "requested_frames": 22}


class _FakePair:
    def unbind(self) -> tuple[object, object]:
        return (object(), object())


def _write_qualification(root: Path, rows: object = None) -> None:
    payload = {
        "subject_identity": NATIVE_NODE_SHA256,
        "rows": _ROWS if rows is None else rows,
    }
    (root / "accepted_qualification.json").write_text(json.dumps(payload), encoding="utf-8")


class RegistrationConditionalityTests(unittest.TestCase):
    def _probe(self, *, enabled: bool) -> subprocess.CompletedProcess[str]:
        root = str(ROOT).replace("\\", "\\\\")
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, "{root}")
            import comfyui_h3_context as package
            ids = sorted(package.NODE_CLASS_MAPPINGS)
            print("\\n".join(ids))
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

    def test_normal_registration_excludes_the_canaries(self) -> None:
        result = self._probe(enabled=False)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(RESUME_SAVE_CANARY_NODE_ID, result.stdout)
        self.assertNotIn(RESUME_LOAD_CANARY_NODE_ID, result.stdout)

    def test_env_gated_registration_includes_the_canaries(self) -> None:
        result = self._probe(enabled=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(RESUME_SAVE_CANARY_NODE_ID, result.stdout)
        self.assertIn(RESUME_LOAD_CANARY_NODE_ID, result.stdout)

    def test_canary_nodes_stay_internal_and_experimental(self) -> None:
        for node in (
            H3LatentResumeCheckpointSaveCanaryNode,
            H3LatentResumeCheckpointLoadCanaryNode,
        ):
            self.assertTrue(node.EXPERIMENTAL)
            self.assertEqual(node.CATEGORY, "h3_context/internal")
            required = node.INPUT_TYPES()["required"]
            self.assertIn("total_steps", required)
            self.assertIn("model_fingerprint", required)
            self.assertNotIn("path", "".join(required))
        save_inputs = H3LatentResumeCheckpointSaveCanaryNode.INPUT_TYPES()["required"]
        self.assertEqual(save_inputs["samples"], ("LATENT",))
        # M20-07: the optional predecessor input must stay optional with an empty default,
        # so every earlier save-canary workflow remains valid byte for byte.
        save_optional = H3LatentResumeCheckpointSaveCanaryNode.INPUT_TYPES()["optional"]
        self.assertEqual(
            save_optional["predecessor_artifact_fingerprint"], ("STRING", {"default": ""})
        )
        load_inputs = H3LatentResumeCheckpointLoadCanaryNode.INPUT_TYPES()["required"]
        self.assertIn("checkpoint_receipt", load_inputs)


class OwnedRootGatingTests(unittest.TestCase):
    def _save(self) -> dict[str, object]:
        return H3LatentResumeCheckpointSaveCanaryNode().execute(
            samples={"samples": _FakePair()},
            artifact_id="resume-checkpoint-001",
            transaction_fingerprint=_FP_B,
            **_boundary_widgets(),
            **_identity_widgets(),
        )

    def test_disabled_lane_refuses_before_anything_else(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(_ENABLE, None)
            with self.assertRaisesRegex(LatentResumeCanaryError, "not enabled"):
                self._save()

    def test_root_misconfiguration_is_a_typed_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned = Path(scratch) / "owned"
            owned.mkdir()
            cases: tuple[tuple[str | None, str], ...] = (
                (None, "not configured"),
                (str(Path(scratch) / "missing"), "unavailable"),
                (str(owned), "not explicitly owned"),
            )
            for value, message in cases:
                env = {_ENABLE: "1"}
                if value is not None:
                    env[_ROOT_VAR] = value
                with mock.patch.dict(os.environ, env, clear=False):
                    if value is None:
                        os.environ.pop(_ROOT_VAR, None)
                    with self.assertRaisesRegex(LatentResumeCanaryError, message):
                        self._save()
            (owned / _MARKER).write_text("wrong\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {_ENABLE: "1", _ROOT_VAR: str(owned)}):
                with self.assertRaisesRegex(LatentResumeCanaryError, "ownership marker is invalid"):
                    self._save()


class QualificationProjectionTests(unittest.TestCase):
    def _owned_env(self, scratch: str) -> tuple[Path, dict[str, str]]:
        owned = Path(scratch) / "owned"
        owned.mkdir()
        (owned / _MARKER).write_text(_MARKER_PAYLOAD, encoding="utf-8")
        return owned, {_ENABLE: "1", _ROOT_VAR: str(owned)}

    def _save(self) -> dict[str, object]:
        return H3LatentResumeCheckpointSaveCanaryNode().execute(
            samples={"samples": _FakePair()},
            artifact_id="resume-checkpoint-001",
            transaction_fingerprint=_FP_B,
            **_boundary_widgets(),
            **_identity_widgets(),
        )

    def test_projection_is_required_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned, env = self._owned_env(scratch)
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(LatentResumeCanaryError, "missing"):
                    self._save()
                target = owned / "accepted_qualification.json"
                target.write_text("x" * (16 * 1024 + 1), encoding="utf-8")
                with self.assertRaisesRegex(LatentResumeCanaryError, "oversized"):
                    self._save()
                target.write_text(json.dumps({"rows": []}), encoding="utf-8")
                with self.assertRaisesRegex(LatentResumeCanaryError, "malformed"):
                    self._save()
                _write_qualification(
                    owned,
                    rows=[{**_ROWS[0], "status": "very_supported"}],
                )
                with self.assertRaisesRegex(LatentResumeCanaryError, "malformed"):
                    self._save()

    def test_valid_projection_reaches_the_torch_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            owned, env = self._owned_env(scratch)
            _write_qualification(owned)
            real_import_module = importlib.import_module

            def import_without_torch(name: str, package: str | None = None) -> object:
                if name == "torch":
                    raise ModuleNotFoundError(name)
                return real_import_module(name, package)

            # IMPORTANT: torch availability is an explicit canary input. Depending on the active
            # venv changes this expected boundary refusal into the later domain-tensor type check.
            with (
                mock.patch.object(
                    importlib,
                    "import_module",
                    side_effect=import_without_torch,
                ),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                with self.assertRaisesRegex(JointAVLatentAdapterError, "torch_runtime_unavailable"):
                    self._save()


class LoadAdmissionPathTests(unittest.TestCase):
    def _receipt_wire(self, boundary: LatentCheckpointBoundary) -> str:
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
                        for entry in _ROWS
                    ),
                )
            )
        )
        descriptor = build_joint_av_latent_descriptor(
            authority=authority,
            video_shape=(1, 24, 1, 1, 1),
            video_dtype="float16",
            audio_shape=(1, 32, 2, 1),
            audio_dtype="float16",
            requested_frames=boundary.requested_frames,
            completed_frames=boundary.requested_frames,
        )
        import time

        now = time.time_ns() // 1_000_000
        partial = begin_joint_av_latent_receipt(
            descriptor=descriptor,
            authority=authority,
            artifact_id="resume-checkpoint-001",
            transaction_fingerprint=_FP_B,
            execution_fingerprint=boundary.fingerprint,
            model_fingerprint=_FP_A,
            runtime_fingerprint=_FP_A,
            settings_fingerprint=_FP_A,
            source_id="segment.resume",
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

    def test_refusal_fails_the_node_with_reason_codes(self) -> None:
        boundary = LatentCheckpointBoundary(
            total_steps=4, completed_steps=2, seed=1, requested_frames=22
        )
        wire = self._receipt_wire(boundary)
        with tempfile.TemporaryDirectory() as scratch:
            owned = Path(scratch) / "owned"
            owned.mkdir()
            (owned / _MARKER).write_text(_MARKER_PAYLOAD, encoding="utf-8")
            _write_qualification(owned)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(LatentResumeCanaryError, "refused: model_mismatch"):
                    H3LatentResumeCheckpointLoadCanaryNode().execute(
                        checkpoint_receipt=wire,
                        **_boundary_widgets(),
                        **_identity_widgets(model_fingerprint=_FP_OTHER),
                    )

    def test_hostile_receipt_strings_are_typed_refusals(self) -> None:
        valid_wire = self._receipt_wire(
            LatentCheckpointBoundary(total_steps=4, completed_steps=2, seed=1, requested_frames=22)
        )
        with tempfile.TemporaryDirectory() as scratch:
            owned = Path(scratch) / "owned"
            owned.mkdir()
            (owned / _MARKER).write_text(_MARKER_PAYLOAD, encoding="utf-8")
            _write_qualification(owned)
            env = {_ENABLE: "1", _ROOT_VAR: str(owned)}
            hostile: tuple[tuple[str, str], ...] = (
                ("", "invalid"),
                ("not json at all", "invalid"),
                (valid_wire[:-40], "invalid"),
                ('{"schema": "h3.context.joint_av_latent_receipt.v1"}', "invalid"),
                ("x" * (64 * 1024 + 1), "oversized"),
            )
            with mock.patch.dict(os.environ, env, clear=False):
                for payload, message in hostile:
                    with self.assertRaisesRegex(LatentResumeCanaryError, message):
                        H3LatentResumeCheckpointLoadCanaryNode().execute(
                            checkpoint_receipt=payload,
                            **_boundary_widgets(),
                            **_identity_widgets(),
                        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
