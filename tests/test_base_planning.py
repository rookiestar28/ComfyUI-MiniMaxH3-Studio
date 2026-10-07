"""M5-03 deterministic Base-mode timeline planner tests."""

from __future__ import annotations

import ast
import json
import unittest
from decimal import Decimal
from pathlib import Path

from comfyui_h3_context.core import (
    AssetRole,
    BasePlanningConfig,
    BaseShot,
    BaseTimelinePlan,
    EvidenceLevel,
    EvidenceOrigin,
    EvidenceRecord,
    EvidenceSource,
    EvidenceSourceKind,
    ExactTextConstraint,
    ExactTextKind,
    HardConstraintSet,
    ImageObservation,
    ImageObservationBatch,
    ImageObservationBatchStatus,
    ImageObservationKind,
    ImageOrientation,
    ImageRegion,
    MediaKind,
    ProviderIdentity,
    RawContextRequest,
    ReferenceAsset,
    ReferenceRegistry,
    SupportStatus,
    TaskMode,
    TimePoint,
    Uncertainty,
    UncertaintyKind,
    VisibleTextCandidate,
    build_reference_registry,
    canonical_fingerprint,
    plan_base_timeline,
    render_base_prompt,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.evidence import Provenance

ROOT = Path(__file__).resolve().parents[1]


def registry(mode: TaskMode) -> ReferenceRegistry:
    assets: list[ReferenceAsset] = []
    if mode in {TaskMode.I2VA, TaskMode.FL2VA}:
        assets.append(ReferenceAsset("first", MediaKind.IMAGE, AssetRole.FIRST_FRAME, 1))
    if mode in {TaskMode.FL2VA, TaskMode.L2VA}:
        assets.append(
            ReferenceAsset(
                "last",
                MediaKind.IMAGE,
                AssetRole.LAST_FRAME,
                len(assets) + 1,
            )
        )
    return build_reference_registry(assets)


def request(
    mode: TaskMode,
    *,
    constraints: HardConstraintSet | None = None,
    duration_seconds: float | None = 5.167,
) -> RawContextRequest:
    constraint_value = HardConstraintSet() if constraints is None else constraints
    return RawContextRequest(
        mode=mode,
        user_intent="A lantern moves through a quiet station.",
        duration_seconds=duration_seconds,
        reference_registry=registry(mode),
        hard_constraints=constraint_value,
    )


def observation_batch() -> ImageObservationBatch:
    source = EvidenceSource(
        EvidenceSourceKind.MEDIA_ASSET,
        "source.first",
        asset_id="first",
        span="frame.1",
    )
    provenance = Provenance(
        source,
        ProviderIdentity.LOCAL,
        EvidenceLevel.EXPERIMENTAL,
        provider_version="observer-1.0.0",
        source_revision="weights-rev-1",
    )
    record = EvidenceRecord(
        "observation.first",
        "a warm light is visible",
        EvidenceOrigin.OBSERVED,
        SupportStatus.UNCERTAIN,
        provenance,
        confidence=Decimal("0.7"),
        uncertainties=(
            Uncertainty(UncertaintyKind.LOW_CONFIDENCE, "The frame is partly occluded."),
        ),
    )
    observation = ImageObservation(
        "observation.first",
        "first",
        ImageObservationKind.SCENE,
        record,
        ImageRegion(0.1, 0.1, 0.5, 0.5),
        ImageOrientation.UP,
    )
    text = VisibleTextCandidate(
        "ocr.first",
        EvidenceRecord(
            "ocr.first",
            "OPEN",
            EvidenceOrigin.OBSERVED,
            SupportStatus.SUPPORTED,
            provenance,
        ),
        ImageRegion(0.2, 0.2, 0.2, 0.1),
        "en",
        1,
        ImageOrientation.UP,
    )
    return ImageObservationBatch(
        "batch.first",
        "h3.image.observation.v1",
        ImageObservationBatchStatus.COMPLETE,
        ("first",),
        observations=(observation,),
        visible_text=(text,),
    )


class BasePlanningTests(unittest.TestCase):
    def test_schema_and_typed_projection_are_bounded(self) -> None:
        config = BasePlanningConfig(seed=17)
        shot = BaseShot(
            "shot_1",
            TimePoint.from_text("0"),
            TimePoint.from_text("5.1666666667"),
            "scene_1",
            asset_ids=("first",),
            evidence_ids=("observation.first",),
        )
        timeline = BaseTimelinePlan(
            "timeline_1",
            TaskMode.I2VA,
            TimePoint.from_text("5.1666666667"),
            (shot,),
            config,
            evidence_ids=("observation.first",),
        )
        self.assertEqual(timeline.schema, "h3.base.timeline.plan.v1")
        self.assertEqual(timeline.to_wire()["config"], config.to_wire())
        schema = json.loads(
            (ROOT / "governance" / "contracts" / "base_timeline_plan_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["$id"], "comfyui-h3-context://contracts/base_timeline_plan_v1.schema.json"
        )
        with self.assertRaises(ContractValidationError):
            BaseShot("bad/path", TimePoint.from_text("0"), TimePoint.from_text("1"), "scene_1")

    def test_each_supported_base_mode_covers_duration_and_owns_frame_assets(self) -> None:
        expected = {
            TaskMode.T2VA: (1, ((),)),
            TaskMode.I2VA: (1, (("first",),)),
            TaskMode.L2VA: (1, (("last",),)),
            TaskMode.FL2VA: (2, (("first",), ("last",))),
        }
        for mode, (shot_count, assets) in expected.items():
            result = plan_base_timeline(request(mode))
            self.assertTrue(result.is_valid, (mode, result.diagnostics))
            assert result.timeline is not None
            assert result.plan is not None
            self.assertEqual(len(result.timeline.shots), shot_count)
            self.assertEqual(tuple(shot.asset_ids for shot in result.timeline.shots), assets)
            self.assertEqual(result.timeline.shots[0].start.seconds, Decimal("0"))
            self.assertEqual(
                result.timeline.shots[-1].end.seconds,
                TimePoint.from_text(str(result.plan.request.effective_duration_seconds)).seconds,
            )
            self.assertEqual(result.plan.intent_graph.validate(), ())
            render_base_prompt(result.plan)

    def test_observed_evidence_is_attached_without_becoming_a_hard_constraint(self) -> None:
        result = plan_base_timeline(request(TaskMode.I2VA), observation_batch())
        self.assertTrue(result.is_valid, result.diagnostics)
        assert result.timeline is not None
        assert result.plan is not None
        self.assertEqual(result.timeline.evidence_ids, ("observation.first", "ocr.first"))
        self.assertEqual(
            tuple(record.evidence_id for record in result.plan.evidence.records),
            ("observation.first", "ocr.first"),
        )
        self.assertEqual(result.plan.hard_constraints, HardConstraintSet())
        self.assertIn(
            "A lantern moves through a quiet station.",
            render_base_prompt(result.plan).text,
        )
        self.assertNotIn("ExactTextConstraint", json.dumps(result.plan.to_wire()))

    def test_partial_and_empty_observations_are_visible_limitations(self) -> None:
        base = observation_batch()
        for status, code in (
            (ImageObservationBatchStatus.PARTIAL, "partial_observation"),
            (ImageObservationBatchStatus.EMPTY, "empty_observation"),
        ):
            degraded = ImageObservationBatch(
                f"batch.{status.value}",
                base.schema,
                status,
                ("first",),
                observations=(
                    () if status is ImageObservationBatchStatus.EMPTY else base.observations
                ),
                visible_text=(),
            )
            result = plan_base_timeline(request(TaskMode.I2VA), degraded)
            self.assertTrue(result.is_valid, result.diagnostics)
            self.assertTrue(any(item.code == code for item in result.limitations))
            assert result.plan is not None
            self.assertTrue(result.plan.limitations)

    def test_invalid_modes_duration_and_observation_ownership_fail_closed(self) -> None:
        ref2va = RawContextRequest(
            TaskMode.REF2VA,
            "unsupported reference video planning",
            reference_registry=ReferenceRegistry.empty(),
        )
        result = plan_base_timeline(ref2va)
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.plan)
        self.assertIn("unsupported_base_mode", {item.code for item in result.diagnostics})

        bad_batch = ImageObservationBatch(
            "batch.bad",
            "h3.image.observation.v1",
            ImageObservationBatchStatus.EMPTY,
            ("unselected",),
        )
        result = plan_base_timeline(request(TaskMode.T2VA), bad_batch)
        self.assertFalse(result.is_valid)
        self.assertIn("observation_asset_unowned", {item.code for item in result.diagnostics})

        mismatch = request(TaskMode.T2VA, duration_seconds=5.0)
        result = plan_base_timeline(mismatch)
        self.assertTrue(result.is_valid, result.diagnostics)
        assert result.plan is not None
        self.assertEqual(result.plan.intent_graph.validate(), ())

    def test_hard_constraints_and_user_intent_survive_deterministic_seeded_runs(self) -> None:
        constraints = HardConstraintSet(
            constraints=(
                ExactTextConstraint("dialogue_1", ExactTextKind.DIALOGUE, "Keep the light on."),
            )
        )
        raw = request(TaskMode.FL2VA, constraints=constraints)
        first = plan_base_timeline(raw, config=BasePlanningConfig(seed=99))
        second = plan_base_timeline(raw, config=BasePlanningConfig(seed=99))
        self.assertTrue(first.is_valid, first.diagnostics)
        self.assertTrue(second.is_valid, second.diagnostics)
        assert first.timeline is not None and second.timeline is not None
        assert first.plan is not None and second.plan is not None
        self.assertEqual(
            canonical_fingerprint(first.timeline.to_wire()),
            canonical_fingerprint(second.timeline.to_wire()),
        )
        self.assertEqual(first.plan.plan_id, second.plan.plan_id)
        prompt = render_base_prompt(first.plan).text
        self.assertIn("<d>Keep the light on.</d>", prompt)
        self.assertIn(raw.user_intent, prompt)

    def test_invalid_manual_timeline_and_optional_runtime_boundary(self) -> None:
        with self.assertRaises(ContractValidationError):
            BaseTimelinePlan(
                "timeline_gap",
                TaskMode.T2VA,
                TimePoint.from_text("5"),
                (
                    BaseShot(
                        "shot_1",
                        TimePoint.from_text("0"),
                        TimePoint.from_text("2"),
                        "scene_1",
                    ),
                    BaseShot(
                        "shot_2",
                        TimePoint.from_text("3"),
                        TimePoint.from_text("5"),
                        "scene_1",
                    ),
                ),
                BasePlanningConfig(),
            )
        source = (ROOT / "comfyui_h3_context" / "core" / "base_planning.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(imports & {"torch", "transformers", "cv2", "requests", "comfy"})


if __name__ == "__main__":
    unittest.main()
