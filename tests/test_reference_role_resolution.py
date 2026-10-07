"""M13-02 explicit cross-asset identity and reference-role resolution tests."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    GraphObservation,
    GraphObservationKind,
    GraphSourceSpan,
    MediaKind,
    Provenance,
    ProviderIdentity,
    ReferenceAsset,
    ReferenceRegistry,
    ReferenceRole,
    ReferenceRoleGraphStatus,
    ReferenceRoleProposal,
    ReferenceRoleResolutionError,
    RoleResolution,
    SupportStatus,
    UncertaintyKind,
    UnifiedEvidenceGraph,
    build_reference_registry,
    build_reference_role_resolution,
    build_unified_evidence_graph,
)
from comfyui_h3_context.core.contracts import ValidationSeverity
from comfyui_h3_context.core.unified_evidence_graph import GraphUncertainty

ROOT = Path(__file__).resolve().parents[1]
FP = {
    "image_1": "sha256:" + "a" * 64,
    "image_2": "sha256:" + "b" * 64,
    "video_1": "sha256:" + "c" * 64,
    "audio_1": "sha256:" + "d" * 64,
}


def registry() -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.SUBJECT_REFERENCE, 1),
            ReferenceAsset("image_2", MediaKind.IMAGE, AssetRole.STYLE_REFERENCE, 2),
            ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 3),
            ReferenceAsset("audio_1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 4),
        )
    )


def evidence(evidence_id: str, asset_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id,
        "explicit identity cue",
        EvidenceOrigin.OBSERVED,
        SupportStatus.SUPPORTED,
        Provenance(
            EvidenceSource(EvidenceSourceKind.MEDIA_ASSET, f"source.{asset_id}", asset_id=asset_id),
            ProviderIdentity.LOCAL,
            EvidenceLevel.EXPERIMENTAL,
            provider_version="fixture-1.0.0",
            source_revision="fixture.rev",
        ),
        Decimal("0.80"),
    )


def graph() -> UnifiedEvidenceGraph:
    observations = tuple(
        GraphObservation(
            f"obs.{asset_id}",
            asset_id,
            MediaKind.IMAGE
            if asset_id.startswith("image")
            else MediaKind.VIDEO
            if asset_id.startswith("video")
            else MediaKind.AUDIO,
            GraphObservationKind.SUBJECT if asset_id == "image_1" else GraphObservationKind.OTHER,
            "explicit observation",
            GraphSourceSpan(asset_id, f"source.{asset_id}", FP[asset_id], 0, 1000),
            evidence_ids=(f"ev.{asset_id}",),
        )
        for asset_id in FP
    )
    return build_unified_evidence_graph(
        observations,
        evidence=tuple(evidence(f"ev.{asset_id}", asset_id) for asset_id in FP),
    )


def proposal(
    proposal_id: str,
    role: ReferenceRole,
    candidates: tuple[str, ...],
    *,
    observation_ids: tuple[str, ...],
    source_asset_ids: tuple[str, ...],
    label: str | None,
    resolution: RoleResolution = RoleResolution.RESOLVED,
    evidence_ids: tuple[str, ...] | None = None,
    uncertainty_ids: tuple[str, ...] = (),
) -> ReferenceRoleProposal:
    return ReferenceRoleProposal(
        proposal_id,
        role,
        candidates,
        label,
        observation_ids,
        source_asset_ids,
        evidence_ids
        if evidence_ids is not None
        else tuple(f"ev.{asset_id}" for asset_id in source_asset_ids),
        resolution=resolution,
        confidence=Decimal("0.80"),
        uncertainty_ids=uncertainty_ids,
    )


class ReferenceRoleResolutionTests(unittest.TestCase):
    def test_schema_labels_and_deterministic_projection(self) -> None:
        schema = json.loads(
            (
                ROOT / "governance" / "contracts" / "reference_role_resolution_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$id"],
            "comfyui-h3-context://contracts/reference_role_resolution_v1.schema.json",
        )
        value = build_reference_role_resolution(
            graph(),
            registry(),
            (
                proposal(
                    "proposal.subject",
                    ReferenceRole.SUBJECT,
                    ("subject.alice",),
                    observation_ids=("obs.image_1", "obs.video_1"),
                    source_asset_ids=("image_1", "video_1"),
                    label="Alice",
                ),
                proposal(
                    "proposal.voice",
                    ReferenceRole.VOICE,
                    ("voice.alice",),
                    observation_ids=("obs.audio_1",),
                    source_asset_ids=("audio_1",),
                    label="speaker A",
                ),
            ),
        )
        repeated = build_reference_role_resolution(
            graph(), registry(), tuple(reversed(value.input_proposals))
        )
        self.assertEqual(value.status, ReferenceRoleGraphStatus.COMPLETE)
        self.assertEqual(value.fingerprint, repeated.fingerprint)
        labels = value.to_wire()["labels"]
        assert isinstance(labels, list)
        self.assertEqual(
            [item["label"] for item in labels],
            ["<Picture 1>", "<Picture 2>", "<Video 1>", "<Audio 1>"],
        )
        self.assertEqual(value.metrics.label_order_errors, 0)

    def test_ambiguous_duplicate_voice_only_and_multi_asset_cases_are_retained(self) -> None:
        unknown = GraphUncertainty(
            "uncertain.lookalike",
            UncertaintyKind.AMBIGUOUS,
            "two look-alike candidates remain",
            ValidationSeverity.WARNING,
        )
        graph_value = build_unified_evidence_graph(
            graph().observations,
            evidence=graph().evidence,
            uncertainties=(unknown,),
        )
        value = build_reference_role_resolution(
            graph_value,
            registry(),
            (
                proposal(
                    "proposal.lookalike",
                    ReferenceRole.SUBJECT,
                    ("subject.a", "subject.b"),
                    observation_ids=("obs.image_1",),
                    source_asset_ids=("image_1",),
                    label=None,
                    resolution=RoleResolution.AMBIGUOUS,
                    uncertainty_ids=("uncertain.lookalike",),
                ),
                proposal(
                    "proposal.voice.only",
                    ReferenceRole.VOICE,
                    ("voice.only",),
                    observation_ids=("obs.audio_1",),
                    source_asset_ids=("audio_1",),
                    label="unknown speaker",
                ),
                proposal(
                    "proposal.multi.asset",
                    ReferenceRole.OBJECT,
                    ("object.cup",),
                    observation_ids=("obs.image_1", "obs.video_1"),
                    source_asset_ids=("image_1", "video_1"),
                    label="cup",
                ),
            ),
        )
        self.assertEqual(value.status, ReferenceRoleGraphStatus.AMBIGUOUS)
        self.assertEqual(len(value.alternatives), 1)
        self.assertEqual(value.alternatives[0].candidate_ids, ("subject.a", "subject.b"))
        self.assertEqual(value.metrics.false_merge_count, 0)
        self.assertEqual(value.metrics.prevented_false_merge_count, 0)
        self.assertNotEqual(value.assignments[0].role, value.assignments[-1].role)

    def test_same_observation_competing_resolved_candidates_conflict_without_merge(self) -> None:
        value = build_reference_role_resolution(
            graph(),
            registry(),
            (
                proposal(
                    "proposal.a",
                    ReferenceRole.SUBJECT,
                    ("subject.a",),
                    observation_ids=("obs.image_1",),
                    source_asset_ids=("image_1",),
                    label="A",
                ),
                proposal(
                    "proposal.b",
                    ReferenceRole.SUBJECT,
                    ("subject.b",),
                    observation_ids=("obs.image_1",),
                    source_asset_ids=("image_1",),
                    label="B",
                ),
            ),
        )
        self.assertEqual(value.status, ReferenceRoleGraphStatus.CONFLICTING)
        self.assertEqual(value.metrics.false_merge_count, 0)
        self.assertEqual(value.metrics.prevented_false_merge_count, 1)
        self.assertEqual(len(value.conflicts), 1)
        self.assertEqual(
            {item.candidate_id for item in value.assignments}, {"subject.a", "subject.b"}
        )

    def test_user_selected_and_asset_role_modality_are_explicit(self) -> None:
        value = build_reference_role_resolution(
            graph(),
            registry(),
            (
                proposal(
                    "proposal.picture",
                    ReferenceRole.PICTURE,
                    ("image_1",),
                    observation_ids=("obs.image_1",),
                    source_asset_ids=("image_1",),
                    label="<Picture 1>",
                    resolution=RoleResolution.USER_SELECTED,
                ),
                proposal(
                    "proposal.video",
                    ReferenceRole.VIDEO,
                    ("video_1",),
                    observation_ids=("obs.video_1",),
                    source_asset_ids=("video_1",),
                    label="<Video 1>",
                    resolution=RoleResolution.USER_SELECTED,
                ),
            ),
        )
        self.assertEqual(value.status, ReferenceRoleGraphStatus.COMPLETE)
        self.assertEqual(
            {item.resolution for item in value.assignments}, {RoleResolution.USER_SELECTED}
        )
        with self.assertRaises(ReferenceRoleResolutionError):
            build_reference_role_resolution(
                graph(),
                registry(),
                (
                    proposal(
                        "proposal.bad",
                        ReferenceRole.PICTURE,
                        ("video_1",),
                        observation_ids=("obs.video_1",),
                        source_asset_ids=("video_1",),
                        label="<Video 1>",
                    ),
                ),
            )

    def test_unknown_references_and_unsafe_values_fail_closed(self) -> None:
        with self.assertRaises(ReferenceRoleResolutionError):
            ReferenceRoleProposal(
                "proposal.bad",
                ReferenceRole.SUBJECT,
                ("subject.a",),
                "https://unsafe",
                ("obs.image_1",),
                ("image_1",),
                ("ev.image_1",),
            )
        with self.assertRaises(ReferenceRoleResolutionError):
            build_reference_role_resolution(
                graph(),
                registry(),
                (
                    proposal(
                        "proposal.unknown",
                        ReferenceRole.SUBJECT,
                        ("subject.a",),
                        observation_ids=("obs.unknown",),
                        source_asset_ids=("image_1",),
                        label="A",
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
