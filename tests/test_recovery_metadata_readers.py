"""Existing owners expose allowed immutable facts without touching execution authority."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from typing import cast
from unittest.mock import patch

from test_authoring_workspace import _Harness
from test_production_workbench import _create, _mutation, _setup

from comfyui_h3_context.adapters.managed_run_registry import ManagedRunRegistry
from comfyui_h3_context.core.durable_workspace_state import (
    StateRecord,
    StateSnapshot,
    encode_snapshot,
)
from comfyui_h3_context.core.managed_run import ManagedRunTrigger
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


class RecoveryMetadataReaderTests(unittest.TestCase):
    def reader(self, registry: object) -> Callable[[], tuple[StateRecord, ...]]:
        result = getattr(registry, "recovery_metadata", None)
        self.assertTrue(
            callable(result), "owner lacks a bounded privacy-safe metadata snapshot port"
        )
        return cast(Callable[[], tuple[StateRecord, ...]], result)

    def test_managed_reader_preserves_zero_based_fact_but_not_handles_or_grants(self) -> None:
        registry = ManagedRunRegistry()
        registry.create("run_private_authority")
        read = self.reader(registry)
        created = read()[0]
        registry.advance(
            "run_private_authority", ManagedRunTrigger.STAGE_CONTEXT, context_revision=1
        )
        current = read()[0]
        assert current.last_transition is not None
        self.assertEqual(current.record_id, created.record_id)
        self.assertEqual(current.last_transition.sequence, 0)
        self.assertEqual(current.revisions.transition, 0)
        payload = encode_snapshot(
            StateSnapshot("owner_" + "a" * 32, 1, current.created_at_ms + 1000, (current,))
        )
        self.assertNotIn(b"run_private_authority", payload)
        self.assertNotIn(b"run_handle", payload)
        self.assertEqual(registry.read("run_private_authority").state.value, "context_ready")

    def test_production_metadata_id_survives_real_mutation_entry_replacement(self) -> None:
        _, registry, seed = _setup()
        projection = registry.dispatch(_create(seed)).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
        read = self.reader(registry)
        first = read()[0]
        updated = registry.dispatch(
            _mutation(
                projection,
                request_id="metadata.select",
                action="set_selection",
                extra={"segment_ids": []},
            )
        )
        self.assertEqual(updated.status, 200)
        assert isinstance(updated.projection, ProductionWorkbenchProjection)
        with patch.object(
            registry,
            "_projection",
            side_effect=AssertionError("must not serialize public authority projection"),
        ):
            second = read()[0]
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(second.revisions.workspace, updated.projection.workspace_revision)
        wire = json.dumps(second.to_wire())
        self.assertNotIn(projection.workspace_handle, wire)
        self.assertNotIn(seed, wire)
        self.assertNotIn("fingerprint", wire)

    def test_authoring_metadata_excludes_source_names_and_editable_timeline(self) -> None:
        harness = _Harness()
        handle, _ = harness.create()
        read = self.reader(harness.registry)
        first = read()[0]
        with patch.object(
            harness.registry, "_projection", side_effect=AssertionError("not a recovery DTO")
        ):
            second = read()[0]
        self.assertEqual(first, second)
        self.assertEqual(second.kind, "authoring")
        self.assertEqual(second.segment_count, 0)
        wire = json.dumps(second.to_wire())
        for forbidden in (handle, "vid-1", "img-1", "report-1", "fingerprint", "clips", "prompt"):
            self.assertNotIn(forbidden, wire)


if __name__ == "__main__":
    unittest.main()
