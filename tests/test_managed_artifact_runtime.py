from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from comfyui_h3_context.adapters.comfyui_input_geometry import (
    INPUT_GEOMETRY_REQUEST_SCHEMA,
    InputGeometryRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    COORDINATOR_ACTION_SCHEMA,
    PREPARED_GRAPH_OBSERVATION_SCHEMA,
    SequenceCoordinatorRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import SidebarWorkspaceRegistry
from comfyui_h3_context.core import ExecutionCorrelation, TaskMode, build_native_h3_wiring
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection
from comfyui_h3_context.nodes import (
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
    H3ContextValidatorNode,
    H3ReferenceRegistryNode,
)

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
HAS_PYAV = importlib.util.find_spec("av") is not None


def _fp(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


def _action(request_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema": COORDINATOR_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": payload,
    }


def _production_workspace() -> tuple[
    ProductionWorkspaceRegistry,
    ProductionWorkbenchProjection,
]:
    request = H3ContextRequestNode().build_request(
        TaskMode.I2VA,
        "A bounded synthetic managed artifact runtime fixture.",
        duration_seconds=8.0,
    )[0]
    references = H3ReferenceRegistryNode().build_registry(first_frame=object())[0]
    plan = H3ContextPlanNode().build_plan(request, references)[0]
    _, _, document = H3ContextCompilerNode().compile(plan)
    report = H3ContextValidatorNode().validate(plan, document)[1]
    wiring = build_native_h3_wiring(report)
    sidebar = SidebarWorkspaceRegistry(max_entries=4, ttl_seconds=60)
    context = sidebar.publish(
        report,
        wiring,
        ExecutionCorrelation("prompt.bootstrap", "node.product.shell"),
    )
    registry = ProductionWorkspaceRegistry(
        seed_claim=sidebar.claim_production_seed,
        max_entries=4,
        ttl_seconds=60,
        terminal_ttl_seconds=60,
    )
    created = registry.dispatch(
        {
            "schema": PRODUCTION_ACTION_SCHEMA,
            "request_id": "production.create.managed.runtime",
            "action": "create_workspace_from_context",
            "payload": {"context_workspace_handle": context.workspace_id},
        }
    )
    projection = created.projection
    if projection is None:  # pragma: no cover - invariant guard
        raise AssertionError("Production fixture was not created")
    assert isinstance(projection, ProductionWorkbenchProjection)
    return registry, projection


def _write_png_header(destination: Path, *, width: int, height: int) -> None:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    destination.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def _encode_fixture(destination: Path) -> bytes:
    if FFMPEG is None:  # pragma: no cover - guarded by skip
        raise AssertionError("ffmpeg is unavailable")
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=512x512:r=24:d=8",
            "-frames:v",
            "192",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-an",
            "-movflags",
            "+faststart",
            "-n",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return destination.read_bytes()


@unittest.skipUnless(HAS_PYAV and FFMPEG is not None, "requires supported-host PyAV and ffmpeg")
class ManagedArtifactRuntimeTests(unittest.TestCase):
    def test_real_mp4_closes_through_inspector_store_receipt_and_production(self) -> None:
        workspace_tmp = ROOT / ".tmp"
        workspace_tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="managed-artifact-", dir=workspace_tmp) as value:
            root = Path(value).resolve()
            input_root = root / "input"
            input_root.mkdir()
            source = input_root / "source.png"
            _write_png_header(source, width=512, height=512)
            geometry_registry = InputGeometryRegistry(
                input_root_factory=lambda: input_root,
                clock=lambda: 100.0,
                token_factory=lambda: "g" * 40,
                fingerprint_secret=b"s" * 32,
            )
            source_identity = geometry_registry.observe(
                {
                    "schema": INPUT_GEOMETRY_REQUEST_SCHEMA,
                    "locator": source.name,
                }
            )
            output_root = root / "output"
            output_root.mkdir()
            artifact = output_root / "managed.mp4"
            payload = _encode_fixture(artifact)
            original_fingerprint = hashlib.sha256(payload).hexdigest()
            private_root = root / "private"
            production_registry, production = _production_workspace()
            coordinator = SequenceCoordinatorRegistry(
                production_registry=production_registry,
                output_root_factory=lambda: output_root,
                private_root_factory=lambda: private_root,
                geometry_receipt_claimant=geometry_registry.claim,
                clock=lambda: 100.0,
                clock_ms=lambda: 100_000,
                token_factory=lambda: "m" * 40,
            )
            prepared_result = coordinator.dispatch(
                _action(
                    "coordinator.prepare.managed.runtime",
                    "prepare_sequence",
                    {
                        "workspace_handle": production.workspace_handle,
                        "expected_workspace_revision": production.workspace_revision,
                        "expected_workspace_fingerprint": production.workspace_fingerprint,
                        "correlation": {
                            "prompt_id": "prompt.bootstrap",
                            "execution_node_id": "node.product.shell",
                        },
                        "observation": {
                            "schema": PREPARED_GRAPH_OBSERVATION_SCHEMA,
                            "route": "existing",
                            "graph_fingerprint": _fp("graph.full"),
                            "compiled_prompt_fingerprint": _fp("prompt.full"),
                            "owned_projection_fingerprint": _fp("graph.owned"),
                            "owned_node_ids": ["1", "8", "45"],
                            "owned_link_ids": ["15", "17", "18"],
                            "model_fingerprint": _fp("model.content-free"),
                            "runtime_fingerprint": _fp("runtime.content-free"),
                            "fingerprint_domain": "output_producing_graph",
                            "expected_frames": 192,
                            "source_identity": source_identity.to_wire(),
                            "timeout_ms": 60_000,
                            "native_anchor_node_id": "node.native.h3",
                        },
                    },
                )
            )
            self.assertIsNotNone(prepared_result.response)
            prepared = prepared_result.response
            if prepared is None:  # pragma: no cover - narrowed above
                raise AssertionError("prepared response missing")
            command = prepared.sequence.eligible_commands[0]
            submitted_result = coordinator.dispatch(
                _action(
                    "coordinator.submit.managed.runtime",
                    "record_submission",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                        "job_id": command.job_id,
                        "transaction_id": command.transaction_id,
                        "graph_fingerprint": command.job.graph_fingerprint,
                        "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                    },
                )
            )
            self.assertIsNotNone(submitted_result.response)
            submitted = submitted_result.response
            if submitted is None:  # pragma: no cover - narrowed above
                raise AssertionError("submitted response missing")
            terminal_result = coordinator.dispatch(
                _action(
                    "coordinator.terminal.managed.runtime",
                    "record_terminal",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": submitted.sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                        "kind": "success",
                    },
                )
            )
            self.assertIsNotNone(terminal_result.response)
            terminal = terminal_result.response
            if terminal is None:  # pragma: no cover - narrowed above
                raise AssertionError("terminal response missing")
            completed_result = coordinator.dispatch(
                _action(
                    "coordinator.artifact.managed.runtime",
                    "record_artifact",
                    {
                        "run_handle": prepared.run_handle,
                        "expected_state_fingerprint": terminal.sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.model.1",
                        "output_node_id": "node.save.video",
                        "locator": {
                            "filename": artifact.name,
                            "subfolder": "",
                            "type": "output",
                        },
                    },
                )
            )
            self.assertIsNotNone(completed_result.response)
            completed = completed_result.response
            if completed is None:  # pragma: no cover - narrowed above
                raise AssertionError("completed response missing")

            self.assertEqual(completed.disposition, "succeeded")
            self.assertEqual(completed.sequence.progress[0].state.value, "succeeded")
            self.assertEqual(
                (completed.production.run_completed, completed.production.run_total),
                (1, 1),
            )
            self.assertEqual(completed.production.run_state, "succeeded")
            self.assertEqual(completed.production.segments[0].task_mode, "i2va")
            delivered_geometry = completed.production.segments[0].delivered_geometry
            self.assertIsNotNone(delivered_geometry)
            if delivered_geometry is None:  # pragma: no cover - narrowed above
                raise AssertionError("delivered geometry missing")
            self.assertEqual(
                delivered_geometry.to_wire(),
                {
                    "format": "mp4",
                    "frame_count": 192,
                    "width": 512,
                    "height": 512,
                },
            )
            self.assertIsNotNone(coordinator._runs[prepared.run_handle].artifact_receipt)
            self.assertEqual(len(tuple((private_root / "receipts").glob("*.json"))), 1)
            self.assertEqual(len(tuple((private_root / "artifacts").glob("*.bin"))), 1)
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                original_fingerprint,
            )
            public_wire = json.dumps(completed.to_wire(), sort_keys=True)
            self.assertNotIn(artifact.name, public_wire)
            self.assertNotIn(source.name, public_wire)
            self.assertNotIn(str(input_root), public_wire)
            self.assertNotIn(str(output_root), public_wire)
            self.assertNotIn(str(private_root), public_wire)


if __name__ == "__main__":
    unittest.main()
