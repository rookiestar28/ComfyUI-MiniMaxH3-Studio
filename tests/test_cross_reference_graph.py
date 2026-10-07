"""M6-03 explicit subject and cross-reference graph contracts without perception runtimes."""

from __future__ import annotations

import ast
import json
import unittest
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    CrossReferenceGraph,
    CrossReferenceGraphStatus,
    CrossReferenceProposal,
    CrossReferenceRequest,
    CrossReferenceResolution,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    LocalAdapterDescriptor,
    LocalAdapterKind,
    LocalAdapterRuntime,
    LocalBudgetGuard,
    LocalDeviceKind,
    LocalDeviceSpec,
    LocalResourceBudget,
    MediaKind,
    ProviderIdentity,
    ReferenceAsset,
    ReferenceEntityKind,
    ReferenceRegistry,
    SupportStatus,
    TaskMode,
    Uncertainty,
    UncertaintyKind,
    build_cross_reference_graph,
    build_reference_registry,
    execute_cross_reference_graph,
)
from comfyui_h3_context.core.cross_reference_graph import (
    CROSS_REFERENCE_GRAPH_SCHEMA,
    ObservationReference,
)
from comfyui_h3_context.core.errors import (
    ContractValidationError,
    CrossReferenceError,
    LocalAdapterBudgetError,
    LocalAdapterCancelledError,
    LocalAdapterMemoryError,
    LocalAdapterTimeoutError,
)
from comfyui_h3_context.core.evidence import Provenance

ROOT = Path(__file__).resolve().parents[1]


def registry() -> ReferenceRegistry:
    return build_reference_registry(
        (
            ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.SUBJECT_REFERENCE, 1),
            ReferenceAsset("video_1", MediaKind.VIDEO, AssetRole.REFERENCE, 2),
            ReferenceAsset("audio_1", MediaKind.AUDIO, AssetRole.AUDIO_SOURCE, 3),
        )
    )


def observations() -> tuple[ObservationReference, ...]:
    return (
        ObservationReference(
            "observation.image.subject", "image_1", "source.image_1", MediaKind.IMAGE
        ),
        ObservationReference(
            "observation.video.scene", "video_1", "source.video_1", MediaKind.VIDEO
        ),
        ObservationReference(
            "observation.audio.voice", "audio_1", "source.audio_1", MediaKind.AUDIO
        ),
    )


def request() -> CrossReferenceRequest:
    return CrossReferenceRequest(TaskMode.REF2VA, registry(), observations())


def evidence(
    evidence_id: str,
    claim: str,
    *,
    asset_id: str = "image_1",
    source_id: str = "source.image_1",
    provider: ProviderIdentity = ProviderIdentity.LOCAL,
    origin: EvidenceOrigin = EvidenceOrigin.ASSISTED_PROPOSAL,
    uncertain: bool = False,
) -> EvidenceRecord:
    uncertainties = (
        (Uncertainty(UncertaintyKind.AMBIGUOUS, "The subject is partly occluded."),)
        if uncertain
        else ()
    )
    source_kind = (
        EvidenceSourceKind.USER_INPUT
        if origin is EvidenceOrigin.USER_DECLARED
        else EvidenceSourceKind.MEDIA_ASSET
    )
    return EvidenceRecord(
        evidence_id,
        claim,
        origin,
        SupportStatus.UNCERTAIN if uncertain else SupportStatus.SUPPORTED,
        Provenance(
            EvidenceSource(
                source_kind,
                source_id,
                asset_id=asset_id if source_kind is EvidenceSourceKind.MEDIA_ASSET else None,
            ),
            ProviderIdentity.MANUAL if origin is EvidenceOrigin.USER_DECLARED else provider,
            EvidenceLevel.EXPERIMENTAL,
            provider_version=None
            if origin is EvidenceOrigin.USER_DECLARED
            else "identity-provider-1.0.0",
            source_revision=None if origin is EvidenceOrigin.USER_DECLARED else "fixture-rev-1",
        ),
        confidence=Decimal("0.75"),
        uncertainties=uncertainties,
    )


def proposal(
    proposal_id: str,
    kind: ReferenceEntityKind,
    candidate_ids: tuple[str, ...],
    *,
    label: str | None,
    observation_ids: tuple[str, ...],
    source_asset_ids: tuple[str, ...],
    resolution: CrossReferenceResolution = CrossReferenceResolution.RESOLVED,
    evidence_values: tuple[EvidenceRecord, ...] | None = None,
    uncertainties: tuple[Uncertainty, ...] = (),
) -> CrossReferenceProposal:
    return CrossReferenceProposal(
        proposal_id,
        kind,
        candidate_ids,
        label,
        observation_ids,
        source_asset_ids,
        evidence_values
        if evidence_values is not None
        else (
            evidence(
                f"{proposal_id}.evidence",
                "an observed identity cue",
                asset_id=source_asset_ids[0],
                source_id=f"source.{source_asset_ids[0]}",
            ),
        ),
        resolution,
        uncertainties,
    )


def resolved_proposals() -> tuple[CrossReferenceProposal, ...]:
    return (
        proposal(
            "proposal.subject",
            ReferenceEntityKind.SUBJECT,
            ("subject.alice",),
            label="Alice",
            observation_ids=("observation.image.subject", "observation.video.scene"),
            source_asset_ids=("image_1", "video_1"),
        ),
        proposal(
            "proposal.voice",
            ReferenceEntityKind.VOICE,
            ("voice.alice",),
            label="speaker A",
            observation_ids=("observation.audio.voice",),
            source_asset_ids=("audio_1",),
        ),
    )


def descriptor() -> LocalAdapterDescriptor:
    return LocalAdapterDescriptor(
        adapter_id="local.cross-reference",
        kind=LocalAdapterKind.REASONING,
        adapter_version="1.0.0",
        minimum_version="1.0.0",
        maximum_version="1.0.0",
        supported_task_modes=frozenset({TaskMode.REF2VA}),
        supported_media=frozenset({MediaKind.IMAGE, MediaKind.VIDEO, MediaKind.AUDIO}),
        supported_devices=frozenset({LocalDeviceKind.AUTO, LocalDeviceKind.CPU}),
        optional_dependencies=("test.identity",),
        output_schema=CROSS_REFERENCE_GRAPH_SCHEMA,
        limits=LocalResourceBudget(2_000_000, 10, 3, 20_000, 128, 1),
        supports_determinism=True,
        supports_seed=True,
        supports_cancellation=True,
    )


class Resolver:
    descriptor = descriptor()

    def __init__(self, graph: CrossReferenceGraph) -> None:
        self.graph = graph
        self.calls = 0

    def analyze(
        self, request_value: CrossReferenceRequest, guard: LocalBudgetGuard
    ) -> CrossReferenceGraph:
        del request_value
        self.calls += 1
        guard.checkpoint()
        return self.graph


class CrossReferenceGraphTests(unittest.TestCase):
    def test_versioned_request_and_canonical_source_nodes(self) -> None:
        current = request()
        self.assertEqual(current.task_mode, TaskMode.REF2VA)
        self.assertEqual(current.selected_asset_ids, ("image_1", "video_1", "audio_1"))
        self.assertEqual(
            current.observation_ids, tuple(item.observation_id for item in observations())
        )
        self.assertEqual(current.to_public_dict()["schema"], CROSS_REFERENCE_GRAPH_SCHEMA)
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "cross_reference_graph_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/cross_reference_graph_v1.schema.json"
        )
        graph = build_cross_reference_graph(current, ())
        self.assertEqual(graph.status, CrossReferenceGraphStatus.EMPTY)
        self.assertEqual(
            {entity.entity_id for entity in graph.entities},
            {"asset.image_1", "asset.video_1", "asset.audio_1"},
        )

    def test_resolved_entities_merge_explicit_candidate_ids_across_observations(self) -> None:
        graph = build_cross_reference_graph(request(), resolved_proposals())
        self.assertEqual(graph.status, CrossReferenceGraphStatus.COMPLETE)
        subject = next(
            entity for entity in graph.entities if entity.kind is ReferenceEntityKind.SUBJECT
        )
        self.assertEqual(subject.entity_id, "subject.alice")
        self.assertEqual(
            subject.observation_ids, ("observation.image.subject", "observation.video.scene")
        )
        self.assertEqual(subject.source_asset_ids, ("image_1", "video_1"))
        self.assertEqual(
            {
                link.entity_id
                for link in graph.links
                if link.entity_kind is ReferenceEntityKind.SUBJECT
            },
            {"subject.alice"},
        )
        self.assertFalse(graph.diagnostics)

    def test_ambiguity_is_retained_and_user_selection_is_explicit(self) -> None:
        ambiguous = proposal(
            "proposal.ambiguous",
            ReferenceEntityKind.SUBJECT,
            ("subject.alice", "subject.bob"),
            label=None,
            observation_ids=("observation.image.subject",),
            source_asset_ids=("image_1",),
            resolution=CrossReferenceResolution.AMBIGUOUS,
            evidence_values=(
                evidence("ambiguous.evidence", "two candidate subjects", uncertain=True),
            ),
            uncertainties=(
                Uncertainty(UncertaintyKind.AMBIGUOUS, "The crop does not disambiguate identity."),
            ),
        )
        graph = build_cross_reference_graph(request(), (ambiguous,))
        self.assertEqual(graph.status, CrossReferenceGraphStatus.AMBIGUOUS)
        entity = next(
            entity for entity in graph.entities if entity.kind is ReferenceEntityKind.SUBJECT
        )
        self.assertIsNone(entity.entity_id)
        self.assertEqual(entity.candidate_ids, ("subject.alice", "subject.bob"))
        self.assertTrue(
            any(diagnostic.code == "ambiguous_identity" for diagnostic in graph.diagnostics)
        )

        selected = proposal(
            "proposal.selected",
            ReferenceEntityKind.SUBJECT,
            ("subject.alice",),
            label="Alice",
            observation_ids=("observation.image.subject",),
            source_asset_ids=("image_1",),
            resolution=CrossReferenceResolution.USER_SELECTED,
            evidence_values=(
                evidence(
                    "selected.evidence",
                    "The caller selected the intended subject.",
                    asset_id="image_1",
                    origin=EvidenceOrigin.USER_DECLARED,
                ),
            ),
        )
        selected_graph = build_cross_reference_graph(request(), (selected,))
        entity = next(
            entity
            for entity in selected_graph.entities
            if entity.kind is ReferenceEntityKind.SUBJECT
        )
        self.assertEqual(selected_graph.status, CrossReferenceGraphStatus.COMPLETE)
        self.assertEqual(entity.resolution, CrossReferenceResolution.USER_SELECTED)

    def test_conflicts_and_duplicate_identity_have_deterministic_diagnostics(self) -> None:
        first = proposal(
            "proposal.first",
            ReferenceEntityKind.SUBJECT,
            ("subject.alice",),
            label="Alice",
            observation_ids=("observation.image.subject",),
            source_asset_ids=("image_1",),
            evidence_values=(evidence("first.evidence", "provider A says Alice"),),
        )
        second = proposal(
            "proposal.second",
            ReferenceEntityKind.SUBJECT,
            ("subject.bob",),
            label="Bob",
            observation_ids=("observation.image.subject",),
            source_asset_ids=("image_1",),
            evidence_values=(
                evidence(
                    "second.evidence",
                    "provider B says Bob",
                    provider=ProviderIdentity.REMOTE_CUSTOM,
                ),
            ),
        )
        graph = build_cross_reference_graph(request(), (first, second))
        self.assertEqual(graph.status, CrossReferenceGraphStatus.CONFLICTING)
        codes = [diagnostic.code for diagnostic in graph.diagnostics]
        self.assertEqual(codes, ["conflicting_provider_result"])
        self.assertTrue(
            all(
                entity.resolution is CrossReferenceResolution.CONFLICTING
                for entity in graph.entities
                if entity.kind is ReferenceEntityKind.SUBJECT
            )
        )

        duplicate = proposal(
            "proposal.duplicate",
            ReferenceEntityKind.VOICE,
            ("subject.alice",),
            label="Alice's voice",
            observation_ids=("observation.audio.voice",),
            source_asset_ids=("audio_1",),
        )
        duplicate_graph = build_cross_reference_graph(request(), (first, duplicate))
        self.assertEqual(duplicate_graph.status, CrossReferenceGraphStatus.CONFLICTING)
        self.assertEqual(duplicate_graph.diagnostics[0].code, "duplicate_identity")

    def test_occlusion_and_missing_evidence_remain_partial_diagnostics(self) -> None:
        unresolved = proposal(
            "proposal.occluded",
            ReferenceEntityKind.OBJECT,
            (),
            label=None,
            observation_ids=("observation.video.scene",),
            source_asset_ids=("video_1",),
            resolution=CrossReferenceResolution.UNRESOLVED,
            evidence_values=(),
            uncertainties=(Uncertainty(UncertaintyKind.MISSING_SOURCE, "Object is occluded."),),
        )
        graph = build_cross_reference_graph(request(), (unresolved,))
        self.assertEqual(graph.status, CrossReferenceGraphStatus.PARTIAL)
        entity = next(
            entity for entity in graph.entities if entity.kind is ReferenceEntityKind.OBJECT
        )
        self.assertEqual(entity.resolution, CrossReferenceResolution.UNRESOLVED)
        self.assertTrue(entity.uncertainties)
        self.assertEqual(graph.diagnostics[0].code, "missing_evidence")
        self.assertEqual(graph.diagnostics[0].severity.value, "warning")

    def test_invalid_ownership_duplicates_and_resolved_missing_evidence_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            ObservationReference("bad", "video_1", "../../private/source", MediaKind.VIDEO)
        with self.assertRaises(ContractValidationError):
            CrossReferenceRequest(TaskMode.T2VA, registry(), observations())
        with self.assertRaises(CrossReferenceError):
            build_cross_reference_graph(
                request(),
                (
                    proposal(
                        "proposal.unknown",
                        ReferenceEntityKind.SUBJECT,
                        ("subject.a",),
                        label="A",
                        observation_ids=("missing.observation",),
                        source_asset_ids=("image_1",),
                    ),
                ),
            )
        with self.assertRaises(CrossReferenceError):
            build_cross_reference_graph(
                request(),
                (
                    proposal(
                        "proposal.no-evidence",
                        ReferenceEntityKind.SUBJECT,
                        ("subject.a",),
                        label="A",
                        observation_ids=("observation.image.subject",),
                        source_asset_ids=("image_1",),
                        evidence_values=(),
                    ),
                ),
            )
        with self.assertRaises(CrossReferenceError):
            build_cross_reference_graph(
                request(), (resolved_proposals()[0], resolved_proposals()[0])
            )

    def test_injected_adapter_reuses_budget_cancellation_seed_and_determinism(self) -> None:
        current = request()
        graph = build_cross_reference_graph(current, resolved_proposals())
        resolver = Resolver(graph)
        first = execute_cross_reference_graph(
            resolver,
            current,
            runtime=LocalAdapterRuntime(),
            device=LocalDeviceSpec(LocalDeviceKind.CPU),
            deterministic_required=True,
            seed=7,
        )
        second = execute_cross_reference_graph(
            resolver, current, deterministic_required=True, seed=7
        )
        self.assertEqual(first.to_wire(), second.to_wire())
        self.assertEqual(resolver.calls, 2)

        class Cancel:
            def is_cancelled(self) -> bool:
                return True

        with self.assertRaises(LocalAdapterCancelledError):
            execute_cross_reference_graph(resolver, current, cancellation_probe=Cancel())
        self.assertEqual(resolver.calls, 2)
        with self.assertRaises(LocalAdapterTimeoutError):
            execute_cross_reference_graph(resolver, current, clock=iter_clock((0.0, 11.0)))
        with self.assertRaises(LocalAdapterMemoryError):
            execute_cross_reference_graph(resolver, current, memory_meter=lambda: 3_000_000)
        oversized = CrossReferenceRequest(
            TaskMode.REF2VA,
            current.reference_registry,
            current.observations,
            estimated_memory_bytes=3_000_000,
        )
        with self.assertRaises(LocalAdapterBudgetError):
            execute_cross_reference_graph(resolver, oversized)

    def test_module_has_no_optional_runtime_imports_and_fixture_is_metadata_only(self) -> None:
        source = (ROOT / "comfyui_h3_context" / "core" / "cross_reference_graph.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(
            imported.isdisjoint(
                {"av", "comfy", "faster_whisper", "librosa", "numpy", "onnxruntime", "torch"}
            )
        )
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "m6_cross_reference_graph.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["schema"], CROSS_REFERENCE_GRAPH_SCHEMA)
        self.assertNotIn("bytes", json.dumps(fixture).casefold())


def iter_clock(values: tuple[float, ...]) -> Callable[[], float]:
    iterator = iter(values)
    last = values[-1]

    def clock() -> float:
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return clock


if __name__ == "__main__":
    unittest.main()
