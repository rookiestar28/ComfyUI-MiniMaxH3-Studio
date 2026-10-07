from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.composition_contract import (
    PublicCompositionSnapshot,
    decode_public_snapshot,
)
from comfyui_h3_context.core.nle_authoring_contract import create_nle_authoring_state
from scripts.m25_16_timeline_fixture import MAX_LINE_BYTES, replay

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/m25_16_timeline_fixture.py"


def _snapshot() -> PublicCompositionSnapshot:
    document = json.loads((ROOT / "tests/fixtures/m25_10_composition_contract_v1.json").read_text())
    return decode_public_snapshot(document["snapshot"])


def _authoring() -> dict[str, Any]:
    snapshot = _snapshot()
    return create_nle_authoring_state(
        project_id=snapshot.project_id,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        edit_capacity_frames=3_600,
        assets=snapshot.assets,
        tracks=snapshot.tracks,
        clips=(),
        audio_extension=snapshot.audio_extension,
        blockers=snapshot.blockers,
    ).to_wire()


def _transaction(
    authoring: dict[str, Any], request: str, kind: str, payload: object
) -> dict[str, Any]:
    return {
        "schema": "h3.context.timeline_transaction.v2",
        "authoring_schema": authoring["schema"],
        "profile_id": authoring["profile_id"],
        "operation_profile_id": authoring["operation_profile_id"],
        "request_id": request,
        "transaction_id": f"tx-{request}",
        "workspace_handle": authoring["workspace_handle"],
        "expected_workspace_revision": authoring["workspace_revision"],
        "expected_timeline_revision": authoring["timeline_revision"],
        "expected_timeline_fingerprint": authoring["timeline_fingerprint"],
        "expected_authoring_fingerprint": authoring["authoring_fingerprint"],
        "commands": [{"kind": kind, "payload": payload}],
    }


def test_v2_replay_returns_canonical_first_insert_and_empty_undo_redo() -> None:
    authoring = _authoring()
    before = replay({"authoring": authoring, "transactions": []})
    assert before["status"] == 200 and before["receipt"] is None
    history = before["history"]
    assert isinstance(history, dict)
    assert history["schema"] == "h3.context.timeline_history_projection.v2"
    assert history["render_snapshot"] is None
    clip = _snapshot().clips[2].to_wire()
    clip.update(clip_id="first-picture", start_frame=18, duration_frames=1)
    insert = _transaction(authoring, "insert", "insert_asset_clip", {"clip": clip})
    inserted = replay({"authoring": authoring, "transactions": [insert]})
    assert inserted["status"] == 200
    receipt = inserted["receipt"]
    assert isinstance(receipt, dict) and receipt["schema"] == "h3.context.timeline_receipt.v2"
    inserted_history = inserted["history"]
    assert isinstance(inserted_history, dict)
    inserted_authoring = inserted_history["authoring"]
    assert isinstance(inserted_authoring, dict)
    assert inserted_authoring["clips"] == [clip]
    render = inserted_history["render_snapshot"]
    assert isinstance(render, dict)
    assert render["timeline_fingerprint"] != inserted_authoring["timeline_fingerprint"]
    undo = _transaction(
        inserted_authoring, "undo", "undo", {"history_cursor": receipt["history_cursor"]}
    )
    undone = replay({"authoring": authoring, "transactions": [insert, undo]})
    undo_history = undone["history"]
    undo_receipt = undone["receipt"]
    assert isinstance(undo_history, dict) and isinstance(undo_receipt, dict)
    assert undo_history["render_snapshot"] is None
    undo_authoring = undo_history["authoring"]
    assert isinstance(undo_authoring, dict) and undo_authoring["clips"] == []
    redo = _transaction(
        undo_authoring, "redo", "redo", {"history_cursor": undo_receipt["history_cursor"]}
    )
    redone = replay({"authoring": authoring, "transactions": [insert, undo, redo]})
    redo_history = redone["history"]
    assert isinstance(redo_history, dict)
    assert redo_history["authoring"]["clips"] == inserted_authoring["clips"]


def test_v2_refusal_preserves_canonical_empty_state() -> None:
    authoring = _authoring()
    transaction = _transaction(authoring, "stale", "select_clips", {"clip_ids": []})
    transaction["expected_authoring_fingerprint"] = "sha256:" + "f" * 64
    reply = replay({"authoring": authoring, "transactions": [transaction]})
    assert reply["status"] == 409 and reply["receipt"] is None
    history = reply["history"]
    assert isinstance(history, dict)
    assert history["authoring"] == authoring
    assert history["render_snapshot"] is None
    assert history["undo_cursor"] is None
    assert history["rejection"] == {"code": "stale_authoring_fingerprint"}


def test_v1_replay_and_served_line_protocol_remain_v1() -> None:
    wire = _snapshot().to_wire()
    expected = replay({"snapshot": wire, "transactions": []})
    assert expected["status"] == 200 and expected["receipt"] is None
    history = expected["history"]
    assert (
        isinstance(history, dict)
        and history["schema"] == "h3.context.timeline_history_projection.v1"
    )
    assert history["snapshot"] == wire
    messages = [{"op": "bootstrap", "snapshot": wire}, {"op": "read"}]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--serve"],
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        capture_output=True,
        check=True,
        cwd=ROOT,
        timeout=15,
    )
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 2
    for reply in replies:
        assert reply["reply"] == expected
        assert set(reply["timing"]) == {"core_ms", "serialize_ms"}
        assert all(value >= 0 for value in reply["timing"].values())


@pytest.mark.parametrize("schema", ["snapshot", "authoring"])
def test_replay_keeps_the_existing_transaction_bound(schema: str) -> None:
    with pytest.raises(ValueError, match="transaction bound exceeded"):
        replay({schema: {}, "transactions": [{}] * 33})


def test_replay_refuses_ambiguous_authority() -> None:
    with pytest.raises(ValueError, match="one authority schema"):
        replay({"snapshot": {}, "authoring": {}, "transactions": []})


def test_cli_and_serve_keep_existing_input_byte_bound() -> None:
    oversized = b" " * (MAX_LINE_BYTES + 1)
    replay_result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=oversized, capture_output=True, cwd=ROOT, timeout=15
    )
    assert replay_result.returncode != 0
    assert b"fixture input bound exceeded" in replay_result.stderr
    served = subprocess.run(
        [sys.executable, str(SCRIPT), "--serve"],
        input=oversized + b"\n",
        capture_output=True,
        check=True,
        cwd=ROOT,
        timeout=15,
    )
    assert json.loads(served.stdout)["reply"] == {"status": 413, "receipt": None, "history": None}
