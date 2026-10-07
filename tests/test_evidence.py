"""M1-05 evidence, provenance, and uncertainty contract tests."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import cast

from comfyui_h3_context.core import (
    CURRENT_SCHEMA_VERSION,
    ContractValidationError,
    EvidenceLevel,
    H3ContextContract,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    RawContextRequest,
    TaskMode,
    normalize_request,
)
from comfyui_h3_context.core.constraints import TimePoint
from comfyui_h3_context.core.evidence import (
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSet,
    EvidenceSource,
    EvidenceSourceKind,
    Provenance,
    SupportStatus,
    Uncertainty,
    UncertaintyKind,
    merge_evidence,
)

ROOT = Path(__file__).resolve().parents[1]


def media_source() -> EvidenceSource:
    return EvidenceSource(
        EvidenceSourceKind.MEDIA_ASSET,
        source_id="asset_1",
        asset_id="asset_1",
        span="ocr.line.1",
        start=TimePoint.from_text("00:01.250"),
        end=TimePoint.from_text("00:02.500"),
    )


def observed_provenance() -> Provenance:
    return Provenance(
        source=media_source(),
        provider=ProviderIdentity.LOCAL,
        evidence_level=EvidenceLevel.EXPERIMENTAL,
        provider_version="vlm-1.2",
        source_revision="weights-rev-7",
    )


class EvidenceTests(unittest.TestCase):
    def test_source_retains_asset_span_and_typed_time_range(self) -> None:
        source = media_source()
        self.assertEqual(source.asset_id, "asset_1")
        self.assertEqual(source.span, "ocr.line.1")
        assert source.start is not None
        self.assertEqual(source.start.seconds, Decimal("1.250"))
        wire = source.to_wire()
        self.assertEqual(wire["start"], {"raw": "00:01.250", "seconds": "1.250"})
        with self.assertRaises(ContractValidationError):
            EvidenceSource(
                EvidenceSourceKind.MEDIA_ASSET,
                "asset_1",
                start=TimePoint.from_text("2"),
                end=TimePoint.from_text("1"),
            )
        with self.assertRaises(ContractValidationError):
            EvidenceSource(EvidenceSourceKind.MEDIA_ASSET, source_id="missing_asset")

    def test_provenance_requires_provider_and_revision_for_assisted_sources(self) -> None:
        provenance = observed_provenance()
        self.assertEqual(provenance.provider, ProviderIdentity.LOCAL)
        self.assertEqual(provenance.source_revision, "weights-rev-7")
        with self.assertRaises(ContractValidationError):
            Provenance(
                media_source(),
                ProviderIdentity.REMOTE_CUSTOM,
                EvidenceLevel.EXPERIMENTAL,
            )
        manual = Provenance(
            EvidenceSource(EvidenceSourceKind.USER_INPUT, "request.user_intent"),
            ProviderIdentity.MANUAL,
            EvidenceLevel.COMMUNITY_RECOMMENDED,
        )
        self.assertIsNone(manual.provider_version)

    def test_observations_and_unsupported_proposals_remain_distinguishable(self) -> None:
        uncertainty = Uncertainty(
            UncertaintyKind.LOW_CONFIDENCE,
            "The face is partly occluded.",
        )
        observed = EvidenceRecord(
            "observation_1",
            "A person is visible near the doorway.",
            EvidenceOrigin.OBSERVED,
            SupportStatus.UNCERTAIN,
            observed_provenance(),
            confidence=Decimal("0.625"),
            uncertainties=(uncertainty,),
        )
        proposal = EvidenceRecord(
            "proposal_1",
            "The person may be the user's named subject.",
            EvidenceOrigin.ASSISTED_PROPOSAL,
            SupportStatus.UNSUPPORTED,
            observed_provenance(),
            uncertainties=(
                Uncertainty(UncertaintyKind.UNSUPPORTED, "No identity evidence was supplied."),
            ),
        )
        self.assertNotEqual(observed.origin, proposal.origin)
        self.assertEqual(proposal.support, SupportStatus.UNSUPPORTED)
        self.assertEqual(proposal.to_wire()["origin"], "assisted_proposal")
        with self.assertRaises(ContractValidationError):
            EvidenceRecord(
                "bad_observation",
                "unsupported claim",
                EvidenceOrigin.OBSERVED,
                SupportStatus.UNSUPPORTED,
                observed_provenance(),
            )
        with self.assertRaises(ContractValidationError):
            EvidenceRecord(
                "bad_proposal",
                "manual guess",
                EvidenceOrigin.ASSISTED_PROPOSAL,
                SupportStatus.SUPPORTED,
                Provenance(
                    EvidenceSource(EvidenceSourceKind.USER_INPUT, "request.user_intent"),
                    ProviderIdentity.MANUAL,
                    EvidenceLevel.COMMUNITY_RECOMMENDED,
                ),
            )

    def test_user_declaration_and_derived_evidence_have_explicit_sources(self) -> None:
        user_record = EvidenceRecord(
            "declared_1",
            "Keep the exact sign text.",
            EvidenceOrigin.USER_DECLARED,
            SupportStatus.SUPPORTED,
            Provenance(
                EvidenceSource(EvidenceSourceKind.USER_INPUT, "request.user_intent"),
                ProviderIdentity.MANUAL,
                EvidenceLevel.COMMUNITY_RECOMMENDED,
            ),
        )
        derived = EvidenceRecord(
            "derived_1",
            "The effective duration is 5.17 seconds.",
            EvidenceOrigin.DERIVED,
            SupportStatus.SUPPORTED,
            Provenance(
                EvidenceSource(EvidenceSourceKind.SYSTEM_DERIVED, "normalization.duration"),
                ProviderIdentity.MANUAL,
                EvidenceLevel.FRAMEWORK_REFERENCE,
                source_revision="c44dea1",
            ),
        )
        self.assertEqual(user_record.provenance.source.kind, EvidenceSourceKind.USER_INPUT)
        self.assertEqual(derived.provenance.source.kind, EvidenceSourceKind.SYSTEM_DERIVED)

    def test_evidence_set_is_immutable_and_merge_is_conflict_closed(self) -> None:
        first = EvidenceRecord(
            "evidence_1",
            "A blue door is visible.",
            EvidenceOrigin.OBSERVED,
            SupportStatus.SUPPORTED,
            observed_provenance(),
        )
        second = EvidenceRecord(
            "evidence_2",
            "The door opens.",
            EvidenceOrigin.ASSISTED_PROPOSAL,
            SupportStatus.UNCERTAIN,
            observed_provenance(),
            uncertainties=(Uncertainty(UncertaintyKind.AMBIGUOUS, "No motion span."),),
        )
        evidence = EvidenceSet((first, second))
        merged = merge_evidence(evidence, EvidenceSet((first,)))
        self.assertEqual(merged.records, (first, second))
        with self.assertRaises(FrozenInstanceError):
            first.claim = "changed"  # type: ignore[misc]
        with self.assertRaises(ContractValidationError):
            merge_evidence(
                evidence,
                EvidenceSet(
                    (
                        EvidenceRecord(
                            "evidence_1",
                            "different claim",
                            EvidenceOrigin.OBSERVED,
                            SupportStatus.SUPPORTED,
                            observed_provenance(),
                        ),
                    )
                ),
            )

    def test_wire_round_trip_and_request_contract_integration(self) -> None:
        evidence = EvidenceSet(
            (
                EvidenceRecord(
                    "evidence_1",
                    "A blue door is visible.",
                    EvidenceOrigin.OBSERVED,
                    SupportStatus.SUPPORTED,
                    observed_provenance(),
                ),
            )
        )
        encoded = json.dumps(evidence.to_wire(), ensure_ascii=False, sort_keys=True)
        self.assertIn("weights-rev-7", encoded)
        contract = H3ContextContract(
            CURRENT_SCHEMA_VERSION,
            ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION),
            TaskMode.T2VA,
            ModelVariant.BASE_FL2VA,
            EvidenceLevel.EXPERIMENTAL,
            ProviderIdentity.LOCAL,
            evidence=evidence,
        )
        self.assertEqual(contract.to_wire()["evidence"], evidence.to_wire())
        result = normalize_request(RawContextRequest(TaskMode.T2VA, "A scene", evidence=evidence))
        self.assertTrue(result.is_valid)
        assert result.request is not None
        self.assertEqual(result.request.evidence, evidence)

    def test_invalid_values_and_optional_import_boundary_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            EvidenceRecord(
                "bad_confidence",
                "claim",
                EvidenceOrigin.OBSERVED,
                SupportStatus.SUPPORTED,
                observed_provenance(),
                confidence=Decimal("1.01"),
            )
        with self.assertRaises(ContractValidationError):
            Uncertainty(UncertaintyKind.AMBIGUOUS, "")
        with self.assertRaises(ContractValidationError):
            EvidenceSet((cast(EvidenceRecord, object()),))

        module = ast.parse(
            (ROOT / "comfyui_h3_context" / "core" / "evidence.py").read_text(encoding="utf-8")
        )
        optional_roots = {"comfy", "torch", "torchaudio", "requests", "httpx", "PIL", "numpy"}
        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(optional_roots.isdisjoint(imported))

    def test_schema_declares_evidence_and_provenance(self) -> None:
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "h3_context_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("evidence", schema["required"])
        self.assertEqual(schema["properties"]["evidence"]["$ref"], "#/$defs/EvidenceSet")
        self.assertIn("Provenance", schema["$defs"])


if __name__ == "__main__":
    unittest.main()
