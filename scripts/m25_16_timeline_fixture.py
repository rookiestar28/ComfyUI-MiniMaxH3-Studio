"""Bounded hermetic browser oracle using the canonical transaction/history core.

Two modes. The default reads one document from stdin and replays at most 32 transactions from the
snapshot every call, which keeps each M25-16 journey stateless. ``--serve`` (M25-21) keeps one
``TimelineHistoryState`` in memory and answers a JSON-lines protocol, so the NLE-STRESS-V1 edit
workload's hundreds of transactions are applied once each by the same canonical core instead of
being replayed quadratically; its answers have exactly the replay mode's shape.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.composition_contract import decode_public_snapshot  # noqa: E402
from comfyui_h3_context.core.errors import ContractValidationError  # noqa: E402
from comfyui_h3_context.core.nle_authoring_contract import decode_nle_authoring_state  # noqa: E402
from comfyui_h3_context.core.timeline_history import (  # noqa: E402
    TimelineHistoryState,
    apply_timeline_transaction,
)
from comfyui_h3_context.core.timeline_history_v2 import (  # noqa: E402
    TimelineHistoryStateV2,
    apply_timeline_transaction_v2,
    timeline_history_projection_v2,
)

MAX_LINE_BYTES = 2_097_152
MAX_SERVED_TRANSACTIONS = 2_048


def _answer(state: TimelineHistoryState, receipt: Any, rejection: str | None) -> dict[str, object]:
    return {
        "status": 200 if rejection is None else 409,
        "receipt": None if receipt is None else receipt.to_wire(),
        "history": {
            "schema": "h3.context.timeline_history_projection.v1",
            "workspace_handle": state.snapshot.workspace_handle,
            "snapshot": state.snapshot.to_wire(),
            "selection": list(state.selection),
            "undo_cursor": state.undo_entries[-1].cursor if state.undo_entries else None,
            "redo_cursor": state.redo_entries[-1].cursor if state.redo_entries else None,
            "rejection": None if rejection is None else {"code": rejection},
        },
    }


def replay(document: dict[str, object]) -> dict[str, object]:
    transactions = document["transactions"]
    if not isinstance(transactions, list) or len(transactions) > 32:
        raise ValueError("fixture transaction bound exceeded")
    if "authoring" in document:
        if "snapshot" in document:
            raise ValueError("fixture must select one authority schema")
        return _replay_v2(document["authoring"], transactions)
    state = TimelineHistoryState.initialize(decode_public_snapshot(document["snapshot"]))
    receipt = None
    rejection = None
    for transaction in transactions:
        try:
            state, receipt = apply_timeline_transaction(state, transaction)
            rejection = None
        except ContractValidationError as error:
            receipt = None
            rejection = getattr(error, "code", "invalid_transaction")
    return _answer(state, receipt, rejection)


def _replay_v2(authoring: object, transactions: list[object]) -> dict[str, object]:
    state = TimelineHistoryStateV2.initialize(decode_nle_authoring_state(authoring))
    receipt = None
    rejection = None
    for transaction in transactions:
        try:
            state, receipt = apply_timeline_transaction_v2(state, transaction)
            rejection = None
        except ContractValidationError as error:
            receipt = None
            rejection = getattr(error, "code", "invalid_transaction")
    return {
        "status": 200 if rejection is None else 409,
        "receipt": None if receipt is None else receipt.to_wire(),
        "history": timeline_history_projection_v2(
            state.authoring.workspace_handle, state, rejection
        ),
    }


def serve() -> None:
    state: TimelineHistoryState | None = None
    applied = 0
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        reply: dict[str, object]
        received_at = time.perf_counter()
        if len(raw) > MAX_LINE_BYTES:
            reply = {"status": 413, "receipt": None, "history": None}
        else:
            try:
                message = json.loads(raw)
                op = message.get("op")
                if op == "bootstrap":
                    state = TimelineHistoryState.initialize(
                        decode_public_snapshot(message["snapshot"])
                    )
                    applied = 0
                    reply = _answer(state, None, None)
                elif state is None:
                    reply = {"status": 503, "receipt": None, "history": None}
                elif op == "read":
                    reply = _answer(state, None, None)
                elif op == "transaction" and applied >= MAX_SERVED_TRANSACTIONS:
                    reply = {"status": 429, "receipt": None, "history": None}
                elif op == "transaction":
                    applied += 1
                    try:
                        state, receipt = apply_timeline_transaction(state, message["transaction"])
                        reply = _answer(state, receipt, None)
                    except ContractValidationError as error:
                        code = getattr(error, "code", "invalid_transaction")
                        reply = _answer(state, None, code)
                else:
                    reply = {"status": 400, "receipt": None, "history": None}
            except Exception as error:  # noqa: BLE001 - keep the line protocol answering
                # A harness defect must surface as a failed request, not as a hung pipe that
                # leaves every later journey step waiting for a line that never comes.
                reply = {
                    "status": 500,
                    "receipt": None,
                    "history": None,
                    "error": type(error).__name__,
                }
        decided_at = time.perf_counter()
        body = json.dumps(reply, separators=(",", ":"))
        serialized_at = time.perf_counter()
        # M25-21 B3-D57: the two costs this process can actually see -- deciding the reply (parse,
        # validate, apply, build the answer) and serializing it -- on this process's own monotonic
        # clock, in milliseconds. Reported beside the caller's own measurement, never subtracted
        # from it: they are different clock domains.
        timing = json.dumps(
            {
                "core_ms": round((decided_at - received_at) * 1000, 3),
                "serialize_ms": round((serialized_at - decided_at) * 1000, 3),
            },
            separators=(",", ":"),
        )
        out.write(f'{{"timing":{timing},"reply":{body}}}'.encode() + b"\n")
        out.flush()


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        payload = sys.stdin.buffer.read(2_097_153)
        if len(payload) > 2_097_152:
            raise ValueError("fixture input bound exceeded")
        print(json.dumps(replay(json.loads(payload))))
