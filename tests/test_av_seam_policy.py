"""M20-09 focused tests: pure seam policy, exact accounting and receipts."""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest
from test_av_reconstruction import _fp, _plan_kwargs

import comfyui_h3_context.core.av_seam_policy as seam_module
from comfyui_h3_context.core.av_reconstruction import (
    AVOutputKind,
    AVPublicationState,
    AVRational,
    AVReceiptOutput,
    AVReconstructionApproval,
    AVReconstructionPlan,
    approve_av_reconstruction_plan,
    build_av_reconstruction_plan,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.av_seam_policy import (
    MAX_AV_SEAM_RECEIPT_BYTES,
    MAX_SEAM_OVERLAP_TARGET_FRAMES,
    MIN_SEAM_OVERLAP_TARGET_FRAMES,
    SAMPLES_PER_TARGET_FRAME,
    SEAM_OVERLAP_GRID_TARGET_FRAMES,
    TARGET_FRAMES_PER_SECOND,
    AVSeamAudioPolicy,
    AVSeamOperation,
    AVSeamPlan,
    AVSeamPolicyError,
    AVSeamReconstructionReceipt,
    AVSeamSpec,
    approve_av_seam_plan,
    build_av_seam_plan,
    decode_av_seam_reconstruction_receipt,
    qualified_seam_capability,
)


def _base(
    count: int = 2,
    *,
    middle_frames: int | None = None,
) -> tuple[AVReconstructionPlan, AVReconstructionApproval]:
    kwargs = _plan_kwargs(count=count)
    if middle_frames is not None:
        descriptors = list(kwargs["media_descriptors"])
        target = descriptors[1]
        seconds = Fraction(middle_frames, TARGET_FRAMES_PER_SECOND)
        end = AVRational(seconds.numerator, seconds.denominator)
        descriptors[1] = replace(
            target,
            video=replace(
                target.video,
                end_time=end,
                decoded_frame_count=middle_frames,
            ),
            audio=replace(
                target.audio,
                end_time=end,
                decoded_sample_count=middle_frames * SAMPLES_PER_TARGET_FRAME,
            ),
        )
        kwargs["media_descriptors"] = tuple(descriptors)
    plan = build_av_reconstruction_plan(**kwargs)
    approval = approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)
    return plan, approval


def _blend_spec(overlap: int, *, master: str = "master.audio") -> AVSeamSpec:
    return AVSeamSpec(
        operation=AVSeamOperation.CROSSFADE,
        overlap_frames=overlap,
        audio_policy=AVSeamAudioPolicy.BLEND,
        predecessor_master_source_id=master,
        successor_master_source_id=master,
    )


class TestDerivedConstants:
    def test_grid_derives_from_the_m20_01_exact_rate_law(self) -> None:
        assert TARGET_FRAMES_PER_SECOND == 30
        assert SAMPLES_PER_TARGET_FRAME == 1600
        assert SEAM_OVERLAP_GRID_TARGET_FRAMES == 15
        assert MIN_SEAM_OVERLAP_TARGET_FRAMES == 15
        assert MAX_SEAM_OVERLAP_TARGET_FRAMES == 150
        # The grid keeps the overlap whole in both native domains.
        native = Fraction(SEAM_OVERLAP_GRID_TARGET_FRAMES * 24, TARGET_FRAMES_PER_SECOND)
        assert native.denominator == 1
        assert native.numerator % 3 == 0

    def test_qualified_seam_capability_binds_the_exact_base_capability(self) -> None:
        capability = qualified_seam_capability()
        base = qualified_ffmpeg_capability()
        assert capability.base_capability_fingerprint == base.fingerprint
        assert capability.fingerprint == qualified_seam_capability().fingerprint
        assert set(capability.required_filters) <= set(base.filters)
        assert "settb" in capability.required_filters
        assert capability.video_transition == "fade"
        assert capability.audio_blend_curve == "tri"

    def test_missing_base_filter_refuses_capability_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = qualified_ffmpeg_capability()
        narrowed = replace(
            base,
            filters=tuple(item for item in base.filters if item != "xfade"),
        )
        monkeypatch.setattr(
            seam_module,
            "qualified_ffmpeg_capability",
            lambda: narrowed,
        )
        with pytest.raises(AVSeamPolicyError, match="seam_capability_filters_unqualified:xfade"):
            qualified_seam_capability()


class TestSeamSpecShapes:
    def test_direct_join_spec_must_stay_empty(self) -> None:
        with pytest.raises(AVSeamPolicyError, match="seam_spec_direct_join_shape"):
            AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN, overlap_frames=15)
        with pytest.raises(AVSeamPolicyError, match="seam_spec_direct_join_shape"):
            AVSeamSpec(
                operation=AVSeamOperation.DIRECT_JOIN,
                predecessor_master_source_id="master.audio",
            )

    def test_crossfade_spec_requires_policy_and_masters(self) -> None:
        with pytest.raises(AVSeamPolicyError, match="seam_spec_audio_policy_missing"):
            AVSeamSpec(operation=AVSeamOperation.CROSSFADE, overlap_frames=15)
        with pytest.raises(AVSeamPolicyError, match="bounded_identifier"):
            AVSeamSpec(
                operation=AVSeamOperation.CROSSFADE,
                overlap_frames=15,
                audio_policy=AVSeamAudioPolicy.BLEND,
            )
        with pytest.raises(AVSeamPolicyError, match="positive_integer"):
            AVSeamSpec(
                operation=AVSeamOperation.CROSSFADE,
                overlap_frames=0,
                audio_policy=AVSeamAudioPolicy.BLEND,
                predecessor_master_source_id="master.audio",
                successor_master_source_id="master.audio",
            )


class TestSeamPlanConstruction:
    def test_all_direct_plan_reproduces_legacy_totals(self) -> None:
        plan, approval = _base()
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),),
            planned_at_ms=320,
        )
        assert seam_plan.crossfade_count == 0
        assert seam_plan.total_output_frames == 60
        assert seam_plan.total_output_samples == 96_000
        assert seam_plan.output_duration == AVRational(2, 1)
        again = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),),
            planned_at_ms=320,
        )
        assert again.fingerprint == seam_plan.fingerprint
        assert again.to_wire() == seam_plan.to_wire()

    def test_blend_crossfade_accounts_exactly(self) -> None:
        plan, approval = _base()
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15),),
            planned_at_ms=320,
        )
        assert seam_plan.crossfade_count == 1
        seam = seam_plan.seams[0]
        assert seam.overlap_frames == 15
        assert seam.overlap_samples == 24_000
        assert seam_plan.total_output_frames == 45
        assert seam_plan.total_output_samples == 72_000
        assert seam_plan.output_duration == AVRational(3, 2)
        assert seam_plan.capability_fingerprint == plan.capability.fingerprint
        assert seam_plan.seam_capability_fingerprint == qualified_seam_capability().fingerprint

    def test_hold_policies_record_the_owning_side(self) -> None:
        plan, approval = _base()
        for policy in (AVSeamAudioPolicy.PREDECESSOR, AVSeamAudioPolicy.SUCCESSOR):
            seam_plan = build_av_seam_plan(
                base_plan=plan,
                base_approval=approval,
                seam_specs=(
                    AVSeamSpec(
                        operation=AVSeamOperation.CROSSFADE,
                        overlap_frames=15,
                        audio_policy=policy,
                        predecessor_master_source_id="master.left",
                        successor_master_source_id="master.right",
                    ),
                ),
                planned_at_ms=320,
            )
            assert seam_plan.seams[0].audio_policy is policy
            assert seam_plan.seams[0].predecessor_master_source_id == "master.left"
            assert seam_plan.seams[0].successor_master_source_id == "master.right"

    @pytest.mark.parametrize(
        ("overlap", "code"),
        (
            (10, "seam_overlap_off_grid"),
            (7, "seam_overlap_off_grid"),
            (165, "seam_overlap_out_of_bounds"),
            (30, "seam_overlap_exceeds_segment"),
            (45, "seam_overlap_exceeds_segment"),
        ),
    )
    def test_invalid_overlaps_refuse(self, overlap: int, code: str) -> None:
        plan, approval = _base()
        with pytest.raises(AVSeamPolicyError, match=code):
            build_av_seam_plan(
                base_plan=plan,
                base_approval=approval,
                seam_specs=(_blend_spec(overlap),),
                planned_at_ms=320,
            )

    def test_interior_consumption_regions_must_stay_disjoint(self) -> None:
        plan, approval = _base(count=3)
        with pytest.raises(AVSeamPolicyError, match="seam_overlap_regions_intersect"):
            build_av_seam_plan(
                base_plan=plan,
                base_approval=approval,
                seam_specs=(_blend_spec(15), _blend_spec(15)),
                planned_at_ms=320,
            )
        widened, widened_approval = _base(count=3, middle_frames=60)
        seam_plan = build_av_seam_plan(
            base_plan=widened,
            base_approval=widened_approval,
            seam_specs=(_blend_spec(15), _blend_spec(15)),
            planned_at_ms=320,
        )
        assert seam_plan.total_output_frames == 30 + 60 + 30 - 30
        assert seam_plan.total_output_samples == seam_plan.total_output_frames * 1600

    def test_blend_with_conflicting_masters_refuses(self) -> None:
        plan, approval = _base()
        with pytest.raises(AVSeamPolicyError, match="master_audio_conflict"):
            build_av_seam_plan(
                base_plan=plan,
                base_approval=approval,
                seam_specs=(
                    AVSeamSpec(
                        operation=AVSeamOperation.CROSSFADE,
                        overlap_frames=15,
                        audio_policy=AVSeamAudioPolicy.BLEND,
                        predecessor_master_source_id="master.left",
                        successor_master_source_id="master.right",
                    ),
                ),
                planned_at_ms=320,
            )

    def test_boundary_and_spec_count_requirements(self) -> None:
        single_plan, single_approval = _base(count=1)
        with pytest.raises(AVSeamPolicyError, match="seam_no_boundary"):
            build_av_seam_plan(
                base_plan=single_plan,
                base_approval=single_approval,
                seam_specs=(),
                planned_at_ms=320,
            )
        plan, approval = _base()
        with pytest.raises(AVSeamPolicyError, match="seam_spec_count_mismatch"):
            build_av_seam_plan(
                base_plan=plan,
                base_approval=approval,
                seam_specs=(
                    AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
                    AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
                ),
                planned_at_ms=320,
            )

    def test_base_identity_is_reverified(self) -> None:
        plan, approval = _base()
        other_kwargs = _plan_kwargs(count=2)
        other_kwargs["planned_at_ms"] = 301
        other_plan = build_av_reconstruction_plan(**other_kwargs)
        other_approval = approve_av_reconstruction_plan(
            other_plan, approved_at_ms=310, expires_at_ms=600
        )
        # An approval bound to a different plan, in both directions.
        with pytest.raises(AVSeamPolicyError, match="seam_base_identity"):
            build_av_seam_plan(
                base_plan=plan,
                base_approval=other_approval,
                seam_specs=(_blend_spec(15),),
                planned_at_ms=320,
            )
        with pytest.raises(AVSeamPolicyError, match="seam_base_identity"):
            build_av_seam_plan(
                base_plan=other_plan,
                base_approval=approval,
                seam_specs=(_blend_spec(15),),
                planned_at_ms=320,
            )


class TestSeamPlanTamperResistance:
    def _seam_plan(self) -> AVSeamPlan:
        plan, approval = _base()
        return build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15),),
            planned_at_ms=320,
        )

    def test_capability_pins_hold_under_replace(self) -> None:
        seam_plan = self._seam_plan()
        with pytest.raises(AVSeamPolicyError, match="seam_capability_changed"):
            replace(
                seam_plan,
                capability_fingerprint=_fp("drifted.capability"),
                plan_fingerprint=None,
            )
        with pytest.raises(AVSeamPolicyError, match="seam_capability_changed"):
            replace(
                seam_plan,
                seam_capability_fingerprint=_fp("drifted.seam.capability"),
                plan_fingerprint=None,
            )

    def test_accounting_tamper_refuses(self) -> None:
        seam_plan = self._seam_plan()
        with pytest.raises(AVSeamPolicyError, match="seam_accounting_mismatch"):
            replace(seam_plan, total_output_frames=60, plan_fingerprint=None)
        with pytest.raises(AVSeamPolicyError, match="seam_segment_rate_mismatch"):
            replace(
                seam_plan,
                segment_emitted_samples=(48_000, 48_001),
                plan_fingerprint=None,
            )
        with pytest.raises(AVSeamPolicyError, match="seam_duration_mismatch"):
            replace(seam_plan, output_duration=AVRational(2, 1), plan_fingerprint=None)

    def test_fingerprint_tamper_refuses(self) -> None:
        seam_plan = self._seam_plan()
        with pytest.raises(AVSeamPolicyError, match="seam_plan_fingerprint_mismatch"):
            replace(seam_plan, plan_fingerprint=_fp("forged.seam.plan"))


class TestSeamApproval:
    def test_approval_flow_and_freshness(self) -> None:
        plan, approval = _base()
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15),),
            planned_at_ms=320,
        )
        seam_approval = approve_av_seam_plan(
            seam_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        seam_approval.assert_executable(
            seam_plan, base_plan=plan, base_approval=approval, now_ms=599
        )
        with pytest.raises(AVSeamPolicyError, match="seam_approval_stale"):
            seam_approval.assert_executable(
                seam_plan, base_plan=plan, base_approval=approval, now_ms=600
            )
        with pytest.raises(AVSeamPolicyError, match="seam_approval_stale"):
            approve_av_seam_plan(
                seam_plan,
                base_approval=approval,
                approved_at_ms=330,
                expires_at_ms=601,
            )

    def test_approval_binds_the_exact_seam_plan_and_base_pair(self) -> None:
        plan, approval = _base()
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15),),
            planned_at_ms=320,
        )
        seam_approval = approve_av_seam_plan(
            seam_plan,
            base_approval=approval,
            approved_at_ms=330,
            expires_at_ms=600,
        )
        other = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),),
            planned_at_ms=320,
        )
        with pytest.raises(AVSeamPolicyError, match="seam_approval_plan_mismatch"):
            seam_approval.assert_executable(
                other, base_plan=plan, base_approval=approval, now_ms=400
            )
        other_kwargs = _plan_kwargs(count=2)
        other_kwargs["planned_at_ms"] = 301
        foreign_plan = build_av_reconstruction_plan(**other_kwargs)
        foreign_approval = approve_av_reconstruction_plan(
            foreign_plan, approved_at_ms=310, expires_at_ms=600
        )
        with pytest.raises(AVSeamPolicyError, match="seam_base_binding_mismatch"):
            seam_approval.assert_executable(
                seam_plan,
                base_plan=foreign_plan,
                base_approval=foreign_approval,
                now_ms=400,
            )


def _receipt() -> AVSeamReconstructionReceipt:
    plan, approval = _base()
    seam_plan = build_av_seam_plan(
        base_plan=plan,
        base_approval=approval,
        seam_specs=(_blend_spec(15),),
        planned_at_ms=320,
    )
    return AVSeamReconstructionReceipt(
        transaction_id="transaction.seam",
        plan_fingerprint=seam_plan.fingerprint,
        approval_fingerprint=_fp("seam.approval"),
        base_plan_fingerprint=plan.fingerprint,
        base_approval_fingerprint=approval.fingerprint,
        capability_fingerprint=plan.capability.fingerprint,
        seam_capability_fingerprint=seam_plan.seam_capability_fingerprint,
        execution_fingerprint=_fp("seam.execution"),
        seams=seam_plan.seams,
        total_output_frames=45,
        total_output_samples=72_000,
        outputs=(
            AVReceiptOutput(
                handle="avout_" + "0" * 64,
                kind=AVOutputKind.RECONSTRUCTION_FULL,
                byte_length=4096,
                content_fingerprint=_fp("aggregate.bytes"),
                video_frame_count=45,
                audio_sample_count=72_000,
                duration=AVRational(3, 2),
            ),
        ),
        publication_state=AVPublicationState.COMPLETE,
        completed_at_ms=500,
    )


class TestSeamReceipt:
    def test_round_trip_is_canonical(self) -> None:
        receipt = _receipt()
        decoded = decode_av_seam_reconstruction_receipt(receipt.to_wire_bytes())
        assert decoded == receipt
        assert decoded.fingerprint == receipt.fingerprint

    def test_accounting_invariants_hold(self) -> None:
        receipt = _receipt()
        with pytest.raises(AVSeamPolicyError, match="seam_receipt_accounting"):
            replace(receipt, total_output_frames=46, receipt_fingerprint=None)
        with pytest.raises(AVSeamPolicyError, match="seam_receipt.outputs"):
            replace(
                receipt,
                outputs=(replace(receipt.outputs[0], kind=AVOutputKind.SEGMENT_EXPORT),),
                receipt_fingerprint=None,
            )
        with pytest.raises(AVSeamPolicyError, match="seam_receipt_fingerprint_mismatch"):
            replace(receipt, receipt_fingerprint=_fp("forged.receipt"))

    def test_decode_rejects_hostile_wire(self) -> None:
        receipt = _receipt()
        wire = receipt.to_wire_bytes()
        with pytest.raises(AVSeamPolicyError, match="seam_receipt_not_canonical"):
            decode_av_seam_reconstruction_receipt(wire + b" ")
        value = json.loads(wire)
        value["extra_member"] = True
        with pytest.raises(AVSeamPolicyError, match="seam_receipt_members"):
            decode_av_seam_reconstruction_receipt(
                json.dumps(value, separators=(",", ":")).encode("utf-8")
            )
        duplicated = wire[:-1] + b',"schema":"forged"}'
        with pytest.raises(AVSeamPolicyError, match="duplicate_seam_receipt_member"):
            decode_av_seam_reconstruction_receipt(duplicated)
        with pytest.raises(AVSeamPolicyError, match="seam_receipt_wire_limit"):
            decode_av_seam_reconstruction_receipt(b" " * (MAX_AV_SEAM_RECEIPT_BYTES + 1))

    def test_seam_order_is_enforced(self) -> None:
        plan, approval = _base(count=3, middle_frames=60)
        seam_plan = build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,
            seam_specs=(_blend_spec(15), _blend_spec(15)),
            planned_at_ms=320,
        )
        receipt = _receipt()
        shuffled = (seam_plan.seams[1], seam_plan.seams[0])
        with pytest.raises(AVSeamPolicyError, match="seam_receipt.seam_order"):
            replace(
                receipt,
                seams=shuffled,
                total_output_frames=90,
                total_output_samples=144_000,
                outputs=(
                    replace(
                        receipt.outputs[0],
                        video_frame_count=90,
                        audio_sample_count=144_000,
                        duration=AVRational(3, 1),
                    ),
                ),
                receipt_fingerprint=None,
            )


def test_public_projection_stays_content_free() -> None:
    receipt = _receipt()
    projection = json.dumps(_as_any(receipt.to_public_dict()))
    assert "content_fingerprint" not in projection
    assert "\\\\" not in projection
    assert ":/" not in projection


def _as_any(value: dict[str, Any]) -> dict[str, Any]:
    return value
