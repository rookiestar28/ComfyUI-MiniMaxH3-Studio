"""M10-01 portable contract-v2 semantic boundary tests."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from comfyui_h3_context.core.contracts import SchemaVersion
from comfyui_h3_context.core.contracts_v2 import (
    CONTRACT_V2_SCHEMA,
    ContextReportV2,
    ContractV2Status,
    Entity,
    EntityKind,
    Event,
    EventKind,
    Observation,
    ObservationKind,
    PortableSourceKind,
    RuntimeReceipt,
    TemporalSpan,
    Timeline,
    Track,
    migrate_v1_report,
)
from comfyui_h3_context.core.errors import ContractV2Error

ROOT = Path(__file__).resolve().parents[1]


class ContractV2Tests(unittest.TestCase):
    def test_json_schema_artifact_matches_contract_version(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "contracts_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema"]["const"], CONTRACT_V2_SCHEMA)
        self.assertEqual(schema["properties"]["version"]["const"], "2.0")
        self.assertIn("Observation", schema["$defs"])
        self.assertIn("Timeline", schema["$defs"])

    def test_v2_semantic_graph_is_immutable_and_reference_closed(self) -> None:
        source = PortableSourceKind.USER_HASH_ONLY
        observation = Observation(
            "obs_1",
            "asset_1",
            ObservationKind.SUBJECT,
            "person",
            source,
            "sha256:" + "a" * 64,
            evidence_ids=("evidence_1",),
            span=TemporalSpan(0, 1200),
            confidence=Decimal("0.75"),
            entity_ids=("entity_1",),
        )
        entity = Entity(
            "entity_1",
            EntityKind.SUBJECT,
            observation_ids=("obs_1",),
            confidence=Decimal("0.75"),
        )
        track = Track("track_1", "entity_1", ("obs_1",), (TemporalSpan(0, 1200),))
        event = Event(
            "event_1",
            EventKind.ACTION,
            1,
            "entity_1",
            ("obs_1",),
            TemporalSpan(0, 1200),
            confidence=Decimal("0.7"),
        )
        timeline = Timeline("timeline_1", (event,), duration_ms=1200)
        report = ContextReportV2(
            "report_1",
            1,
            ContractV2Status.PARTIAL,
            asset_ids=("asset_1",),
            observations=(observation,),
            entities=(entity,),
            tracks=(track,),
            timeline=timeline,
            diagnostics=("missing_audio",),
        )
        wire = report.to_wire()
        self.assertEqual(wire["schema"], CONTRACT_V2_SCHEMA)
        self.assertEqual(json.loads(json.dumps(wire)), wire)
        self.assertEqual(report.claim_ceiling, "partial")

    def test_portable_contract_rejects_locator_and_runtime_leaks(self) -> None:
        with self.assertRaises(ContractV2Error):
            Observation(
                "obs_1",
                "asset_1",
                ObservationKind.SCENE,
                "https://private.example/frame",
                PortableSourceKind.PUBLIC_DEREFERENCEABLE,
                "sha256:" + "a" * 64,
            )
        with self.assertRaises(ContractV2Error):
            RuntimeReceipt(
                "receipt_1",
                "comfyui_native",
                "ok",
                metadata=(("path", "https://private.example"),),
            )

    def test_timeline_rejects_duplicate_or_unknown_observations(self) -> None:
        event = Event(
            "event_1",
            EventKind.SCENE,
            1,
            "entity_1",
            ("obs_missing",),
            TemporalSpan(0, 100),
        )
        with self.assertRaises(ContractV2Error):
            ContextReportV2(
                "report_1",
                1,
                ContractV2Status.COMPLETE,
                asset_ids=("asset_1",),
                timeline=Timeline("timeline_1", (event,), duration_ms=100),
            )

    def test_v1_migration_is_explicit_and_preserves_identity(self) -> None:
        class LegacyReport:
            report_id = "legacy_report"
            schema_version = SchemaVersion(1, 0)
            request = type("Request", (), {"assets": ()})()
            has_errors = False

        migrated = migrate_v1_report(cast(Any, LegacyReport()))
        self.assertEqual(migrated.report_id, "legacy_report")
        self.assertEqual(migrated.migrated_from, "h3.context.report.v1")
        self.assertEqual(migrated.status, ContractV2Status.PARTIAL)
        self.assertIn("v1_migration_requires_observations", migrated.diagnostics)


if __name__ == "__main__":
    unittest.main()
