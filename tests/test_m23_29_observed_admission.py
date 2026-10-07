"""M23-29 D11 regressions for identity-only admission and observed artifacts."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.comfyui_input_geometry import (
    INPUT_GEOMETRY_RECEIPT_SCHEMA,
    INPUT_GEOMETRY_REQUEST_SCHEMA,
    GeometryPreflightError,
    InputGeometryRegistry,
)
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    PREPARED_GRAPH_OBSERVATION_SCHEMA,
    ObservedVideoArtifact,
    PreparedGraphObservation,
)


def _png_header(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def _registry(root: Path) -> InputGeometryRegistry:
    return InputGeometryRegistry(
        input_root_factory=lambda: root,
        clock=lambda: 100.0,
        token_factory=lambda: "v" * 40,
        fingerprint_secret=b"s" * 32,
    )


def test_v2_receipt_binds_only_private_source_identity_and_is_one_time(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "source.png").write_bytes(_png_header(120, 160))
    registry = _registry(input_root)

    assert INPUT_GEOMETRY_REQUEST_SCHEMA == "h3.context.input_geometry.request.v2"
    assert INPUT_GEOMETRY_RECEIPT_SCHEMA == "h3.context.input_geometry.receipt.v2"
    receipt = registry.observe({"schema": INPUT_GEOMETRY_REQUEST_SCHEMA, "locator": "source.png"})

    assert receipt.to_wire() == {
        "schema": INPUT_GEOMETRY_RECEIPT_SCHEMA,
        "receipt_handle": "ig_" + "v" * 40,
        "source_fingerprint": receipt.source_fingerprint,
    }
    assert "source.png" not in repr(receipt)
    assert registry.claim(receipt.to_wire()) == receipt
    with pytest.raises(GeometryPreflightError, match="^geometry_receipt_stale$"):
        registry.claim(receipt.to_wire())


def test_v1_prediction_request_fails_closed_instead_of_retaining_side_branch_logic(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "source.png").write_bytes(_png_header(120, 160))

    with pytest.raises(GeometryPreflightError, match="^invalid_request$"):
        _registry(input_root).observe(
            {
                "schema": "h3.context.input_geometry.request.v1",
                "locator": "source.png",
                "upscale_method": "nearest-exact",
                "megapixels": 0.8,
                "resolution_steps": 32,
                "topology_fingerprint": "sha256:" + "a" * 64,
            }
        )


def test_prepared_observation_v4_carries_frames_and_source_identity_only() -> None:
    observation = PreparedGraphObservation.from_wire(
        {
            "schema": PREPARED_GRAPH_OBSERVATION_SCHEMA,
            "route": "existing",
            "graph_fingerprint": "sha256:" + "1" * 64,
            "compiled_prompt_fingerprint": "sha256:" + "2" * 64,
            "owned_projection_fingerprint": "sha256:" + "3" * 64,
            "owned_node_ids": ["node.context", "node.anchor"],
            "owned_link_ids": ["link.prompt"],
            "model_fingerprint": "sha256:" + "4" * 64,
            "runtime_fingerprint": "sha256:" + "5" * 64,
            "fingerprint_domain": "output_producing_graph",
            "expected_frames": 192,
            "source_identity": None,
            "timeout_ms": 3_600_000,
            "native_anchor_node_id": "node.anchor",
        }
    )

    assert PREPARED_GRAPH_OBSERVATION_SCHEMA == "h3.context.prepared_graph_observation.v4"
    assert observation.expected_frames == 192
    assert observation.source_identity is None
    wire = observation.to_wire()
    assert not {
        "expected_format",
        "expected_shape",
        "geometry_authority",
        "source_geometry",
        "artifact_sink_node_id",
    }.intersection(wire)


def test_observed_video_artifact_requires_full_safe_delivered_shape() -> None:
    observed = ObservedVideoArtifact(format_label="webm", shape=(192, 704, 1216, 3))

    assert observed.format_label == "webm"
    assert observed.shape == (192, 704, 1216, 3)
    with pytest.raises(ValueError):
        ObservedVideoArtifact(format_label="webm", shape=(192, 0, 1216, 3))
