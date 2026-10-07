"""Recovery metadata is strict plain data, never reconstructed execution authority."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from comfyui_h3_context.adapters.managed_run_registry import ManagedRunRegistry
from comfyui_h3_context.core.managed_run import MANAGED_RUN_MACHINE

MODULE = "comfyui_h3_context.core.durable_workspace_state"
OWNER = "owner_" + "a" * 32
RECORD = "record_" + "b" * 32


def record(state: str = "created") -> dict[str, Any]:
    fact = None
    if state != "created":
        edge = next(row for row in MANAGED_RUN_MACHINE.transitions if row.target == state)
        fact = {
            "sequence": 3,
            "trigger": edge.trigger,
            "source": edge.source[0],
            "target": edge.target,
            "guard": edge.guard,
        }
    return {
        "record_id": RECORD,
        "kind": "managed_run",
        "created_at_ms": 1000,
        "closed_at_ms": None,
        "segment_count": 1,
        "state": state,
        "revisions": {
            "workspace": None,
            "reference": None,
            "timeline": None,
            "context": 1,
            "transition": 0 if fact is None else 3,
        },
        "last_transition": fact,
    }


def snapshot(state: str = "created") -> dict[str, Any]:
    return {
        "schema": "h3.context.workspace_state.v1",
        "owner_id": OWNER,
        "revision": 1,
        "saved_at_ms": 2000,
        "records": [record(state)],
    }


def payload(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


class DurableStateContractTests(unittest.TestCase):
    def core(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec(MODULE),
            "missing privacy-safe state decoding and non-executable recovery boundary",
        )
        return importlib.import_module(MODULE)

    def test_allowed_roundtrip_is_immutable_and_does_not_alias_input(self) -> None:
        core = self.core()
        wire = snapshot("running")
        decoded = core.decode_snapshot(payload(wire), expected_owner=OWNER)
        wire["records"][0]["state"] = "terminal_succeeded"
        self.assertEqual(decoded.to_wire(), snapshot("running"))
        self.assertEqual(
            core.decode_snapshot(core.encode_snapshot(decoded), expected_owner=OWNER), decoded
        )
        with self.assertRaises((AttributeError, TypeError)):
            decoded.revision = 2

    def test_shared_browser_fixture_is_admitted_by_the_real_backend_codec(self) -> None:
        core = self.core()
        response = json.loads(
            (Path(__file__).parent / "fixtures/workspace_state_v1.json").read_bytes()
        )
        projection = response["projection"]
        decoded = core.decode_snapshot(
            payload(
                {
                    "schema": core.STATE_SCHEMA,
                    "owner_id": OWNER,
                    "revision": projection["revision"],
                    "saved_at_ms": projection["saved_at_ms"],
                    "records": projection["records"],
                }
            ),
            expected_owner=OWNER,
        )
        self.assertEqual(decoded.to_wire()["records"], projection["records"])
        self.assertEqual(projection["count"], len(decoded.records))
        self.assertIsNone(response["recovered"])

    def test_unknown_fields_cannot_carry_user_content_or_runtime_authority(self) -> None:
        core = self.core()
        for key in (
            "prompt",
            "media",
            "path",
            "provider",
            "credential",
            "lease",
            "run_handle",
            "workspace_handle",
            "prompt_id",
            "plan",
            "source_fingerprint",
        ):
            for location in ("root", "record", "revisions", "transition"):
                with self.subTest(key=key, location=location):
                    value = snapshot("running")
                    parent = value if location == "root" else value["records"][0]
                    if location == "revisions":
                        parent = parent["revisions"]
                    elif location == "transition":
                        parent = parent["last_transition"]
                    parent[key] = "private-value-must-not-be-admitted"
                    with self.assertRaises(core.DurableStateError):
                        core.decode_snapshot(payload(value), expected_owner=OWNER)

    def test_strict_json_and_shape_refusals(self) -> None:
        core = self.core()
        cases = [
            b'{"schema":"x","schema":"y"}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b"\xff",
            b'"text"',
            b"[]",
            b"null",
            b"[" * 1100 + b"]" * 1100,
            b" " * (core.MAX_SNAPSHOT_BYTES + 1),
        ]
        for data in cases:
            with self.subTest(size=len(data)):
                with self.assertRaises(core.DurableStateError):
                    core.decode_snapshot(data, expected_owner=OWNER)
        for bad in (True, -1, 1.5, "1", 10**20):
            value = snapshot()
            value["revision"] = bad
            with self.subTest(revision=bad), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(value), expected_owner=OWNER)

    def test_foreign_owner_unknown_version_and_authority_like_ids_refuse(self) -> None:
        core = self.core()
        changes = [("schema", "h3.context.workspace_state.v2"), ("owner_id", "owner_" + "c" * 32)]
        for key, value in changes:
            wire = snapshot()
            wire[key] = value
            with self.subTest(key=key), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(wire), expected_owner=OWNER)
        wire = snapshot()
        for identifier in ("pw_live", "ws_live", "record_../a", "record_" + "a" * 33):
            wire["records"][0]["record_id"] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(wire), expected_owner=OWNER)

    def test_transition_must_be_legal_and_explain_its_own_target_state(self) -> None:
        core = self.core()
        wire = snapshot("terminal_succeeded")
        for key, value in (
            ("source", "created"),
            ("target", "running"),
            ("trigger", "start_again"),
            ("guard", "trust_digest"),
            ("sequence", True),
            ("sequence", 65),
        ):
            candidate = copy.deepcopy(wire)
            candidate["records"][0]["last_transition"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(candidate), expected_owner=OWNER)
        wire["records"][0]["last_transition"] = None
        with self.assertRaises(core.DurableStateError):
            core.decode_snapshot(payload(wire), expected_owner=OWNER)

    def test_each_inflight_state_recovers_as_unknown_without_registry_installation(self) -> None:
        core = self.core()
        registry = ManagedRunRegistry()
        registry.create("run_live_unrelated")
        before = registry.read("run_live_unrelated")
        handles = set()
        for state in ("submitted", "running", "artifact_recorded"):
            decoded = core.decode_snapshot(payload(snapshot(state)), expected_owner=OWNER)
            restored = core.recover_record(decoded.records[0])
            self.assertEqual(restored["state"], "terminal_unknown_ownership")
            self.assertEqual(restored["source_status"], "source_reauthorization_required")
            self.assertTrue(restored["recovery_handle"].startswith("recovery_"))
            self.assertFalse(restored["executable"])
            self.assertNotIn("run_handle", restored)
            handles.add(restored["recovery_handle"])
        self.assertEqual(len(handles), 3)
        self.assertIs(registry.read("run_live_unrelated"), before)

    def test_valid_terminal_fact_stays_terminal_but_never_executable(self) -> None:
        core = self.core()
        for state in (
            "terminal_succeeded",
            "terminal_failed",
            "terminal_cancelled",
            "terminal_unknown_ownership",
            "expired",
        ):
            decoded = core.decode_snapshot(payload(snapshot(state)), expected_owner=OWNER)
            first = core.recover_record(decoded.records[0])
            second = core.recover_record(decoded.records[0])
            self.assertEqual(first["state"], state)
            self.assertFalse(first["executable"])
            self.assertNotEqual(first["recovery_handle"], second["recovery_handle"])

    def test_duplicate_ids_counts_and_cross_kind_revisions_refuse(self) -> None:
        core = self.core()
        for size in (2, 65):
            wire = snapshot()
            wire["records"] *= size
            with self.subTest(size=size), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(wire), expected_owner=OWNER)
        for key, value in (
            ("segment_count", True),
            ("segment_count", 65),
            ("closed_at_ms", 999),
            ("kind", "executable"),
        ):
            wire = snapshot()
            wire["records"][0][key] = value
            with self.subTest(key=key), self.assertRaises(core.DurableStateError):
                core.decode_snapshot(payload(wire), expected_owner=OWNER)
        wire = snapshot()
        wire["records"][0]["revisions"]["workspace"] = 1
        with self.assertRaises(core.DurableStateError):
            core.decode_snapshot(payload(wire), expected_owner=OWNER)

    def test_workspace_lifecycle_requires_closed_time_iff_closed_state(self) -> None:
        core = self.core()
        for kind in ("production", "authoring"):
            for state, closed in (("workspace_active", 1500), ("workspace_closed", None)):
                wire = snapshot()
                row = wire["records"][0]
                row.update(
                    kind=kind,
                    state=state,
                    closed_at_ms=closed,
                    segment_count=0,
                    revisions={
                        "workspace": 1,
                        "reference": 0 if kind == "authoring" else None,
                        "timeline": 0 if kind == "authoring" else None,
                        "context": None,
                        "transition": 0,
                    },
                )
                with (
                    self.subTest(kind=kind, state=state),
                    self.assertRaises(core.DurableStateError),
                ):
                    core.decode_snapshot(payload(wire), expected_owner=OWNER)


if __name__ == "__main__":
    unittest.main()
