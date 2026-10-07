"""M26-05 public assembly refusal and no-effect qualification."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from test_production_import import _m26_subject

from comfyui_h3_context.adapters.comfyui_production_workspace import (
    PRODUCTION_ACTION_SCHEMA,
    ProductionWorkbenchError,
    decode_production_action_json,
)
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore
from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.production_duration import SegmentationPolicyV1
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection

_FINGERPRINT = "sha256:" + "a" * 64


def _snapshot(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    )


def _assemble_action(
    projection: ProductionWorkbenchProjection,
    request_id: str,
) -> dict[str, object]:
    return {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": request_id,
        "action": "assemble_sequence",
        "payload": {
            "workspace_handle": projection.workspace_handle,
            "workspace_id": projection.workspace_id,
            "expected_workspace_revision": projection.workspace_revision,
            "expected_workspace_fingerprint": projection.workspace_fingerprint,
            "managed_sequence_fingerprint": _FINGERPRINT,
            "artifact_receipt_fingerprints": [_FINGERPRINT],
            "cut_boundary_receipt_fingerprints": [],
            "assembly_capability_fingerprint": _FINGERPRINT,
            "output_profile_id": "legacy_av_30fps_48khz_stereo",
        },
    }


def test_public_assembly_refusals_do_not_allocate_without_authorized_runtime(
    tmp_path: Path,
) -> None:
    # IMPORTANT: compose the accepted canonical materialization/qualification fixture here;
    # replacing it with a synthetic projection would stop testing the real Production registry.
    _context, _proposal, registry, imported, _authority, sequence, receipts = _m26_subject(
        SegmentationPolicyV1.FIXED_15
    )
    artifact_store = PrivateSegmentArtifactStore(
        tmp_path / "artifacts",
        clock_ms=lambda: 2_000,
    )
    current = registry.replace_generation_authority(
        imported.workspace_handle,
        expected_workspace_revision=imported.workspace_revision,
        expected_workspace_fingerprint=imported.workspace_fingerprint,
        expected_sequence_state_fingerprint=None,
        generation_sequence=sequence,
        artifact_receipts=receipts,
        artifact_store=artifact_store,
    )
    baseline = _snapshot(tmp_path)
    assert current.assembly.state == "unavailable"
    assert current.assembly.failure_code == "media_runtime_not_authorized"
    assert "assemble_sequence" not in current.allowed_actions

    decoded = decode_production_action_json(
        canonical_bytes(_assemble_action(current, "m26.public.authorization"))
    )
    with pytest.raises(ProductionWorkbenchError, match="assembly_authority_mismatch") as refused:
        registry.dispatch(decoded)
    assert refused.value.status == 409

    stale = _assemble_action(current, "m26.public.stale")
    cast(dict[str, object], stale["payload"])["expected_workspace_revision"] = (
        current.workspace_revision - 1
    )
    with pytest.raises(ProductionWorkbenchError, match="stale_workspace") as refused:
        registry.dispatch(decode_production_action_json(canonical_bytes(stale)))
    assert refused.value.status == 409
    assert refused.value.projection == current

    foreign = _assemble_action(current, "m26.public.foreign")
    cast(dict[str, object], foreign["payload"])["workspace_handle"] = "pw_" + "z" * 43
    with pytest.raises(ProductionWorkbenchError, match="workspace_unavailable") as refused:
        registry.dispatch(decode_production_action_json(canonical_bytes(foreign)))
    assert refused.value.status == 404

    selection = {
        "schema": PRODUCTION_ACTION_SCHEMA,
        "request_id": "m26.public.replay",
        "action": "set_selection",
        "payload": {
            "workspace_handle": current.workspace_handle,
            "expected_workspace_revision": current.workspace_revision,
            "expected_workspace_fingerprint": current.workspace_fingerprint,
            "segment_ids": list(current.selected_segment_ids),
        },
    }
    selected = registry.dispatch(selection).projection
    assert isinstance(selected, ProductionWorkbenchProjection)
    assert registry.dispatch(selection).projection == selected
    conflict = _assemble_action(selected, "m26.public.replay")
    with pytest.raises(ProductionWorkbenchError, match="request_id_conflict") as refused:
        registry.dispatch(decode_production_action_json(canonical_bytes(conflict)))
    assert refused.value.status == 409

    malformed = _assemble_action(selected, "m26.public.malformed")
    malformed["private_path"] = "must-not-enter-the-service"
    with pytest.raises(ValueError, match="production action must be one closed object"):
        decode_production_action_json(canonical_bytes(malformed))

    entry = registry._entries[current.workspace_handle]
    assert entry.assembly_job is None
    assert registry._assembly_executor is None
    assert _snapshot(tmp_path) == baseline
