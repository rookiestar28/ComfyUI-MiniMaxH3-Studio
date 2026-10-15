"""Native-rate assembly target: minting, admission and the unchanged legacy identities."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from comfyui_h3_context.core.av_reconstruction import (
    AVAudioDescriptor,
    AVBoundaryEvidence,
    AVMediaDescriptor,
    AVOperation,
    AVRational,
    AVReconstructionError,
    AVReconstructionPlan,
    AVTargetProfile,
    AVVideoDescriptor,
    approve_av_reconstruction_plan,
    build_m26_derived_av_reconstruction_plan,
    is_native_av_target_profile,
    native_av_target_profile,
    native_av_target_profile_rule_fingerprint,
    qualified_av_limits,
    qualified_av_target_profile,
    qualified_ffmpeg_capability,
)
from comfyui_h3_context.core.av_seam_policy import AVSeamPolicyError, build_av_seam_plan
from comfyui_h3_context.core.continuity_handoff import ContinuityBoundaryReceipt, ContinuityMode
from comfyui_h3_context.core.m26_assembly import (
    M26_NATIVE_OUTPUT_PROFILE_ID,
    M26_OUTPUT_PROFILE_ID,
    M26_OUTPUT_PROFILE_IDS,
    build_production_assembly_capability,
    m26_output_profile_fingerprint,
)
from comfyui_h3_context.core.selective_rerun import SelectiveRerunDisposition

#: Identities computed on the base commit before the native profile existed. They are bound into
#: capabilities, authorizations and stored receipts, so they must never move.
LEGACY_CAPABILITY = "sha256:e184651bc1f8376502735bc582a87c3e880b4b5581e9d07ef95d09f8082aec8e"
LEGACY_TARGET = "sha256:47537ec17069b1119cb9d6ea2c8a71914d4cd8957a43e678d3712ab0a76aa44c"
LEGACY_ASSEMBLY_CAPABILITY = (
    "sha256:4509cbabf7b8837f4b34e390755db39c891c629a6df4cd080b0d56200ef0f170"
)
STORE = "sha256:" + "a" * 64


def _fp(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


def test_legacy_identities_are_unchanged_by_the_native_profile() -> None:
    assert qualified_ffmpeg_capability().fingerprint == LEGACY_CAPABILITY
    assert qualified_av_target_profile().fingerprint == LEGACY_TARGET
    legacy = build_production_assembly_capability(store_identity_fingerprint=STORE)
    assert legacy.output_profile_id == M26_OUTPUT_PROFILE_ID
    assert legacy.fingerprint == LEGACY_ASSEMBLY_CAPABILITY
    # New assemblies default to the native profile; legacy stays selectable.
    assert M26_OUTPUT_PROFILE_IDS == (M26_NATIVE_OUTPUT_PROFILE_ID, M26_OUTPUT_PROFILE_ID)


def test_native_profile_keeps_geometry_and_takes_the_native_rates_only() -> None:
    native = native_av_target_profile(864, 480)
    legacy = qualified_av_target_profile()
    assert (native.width, native.height) == (864, 480)
    assert native.frame_rate == AVRational(24, 1)
    assert (native.sample_rate, native.audio_time_base) == (32_000, AVRational(1, 32_000))
    changed = {"width", "height", "frame_rate", "sample_rate", "audio_time_base"}
    native_wire, legacy_wire = native.to_wire(), legacy.to_wire()
    assert {key for key in native_wire if native_wire[key] != legacy_wire[key]} == changed
    assert is_native_av_target_profile(native)
    assert not is_native_av_target_profile(legacy)


@pytest.mark.parametrize(
    ("width", "height"),
    [(865, 480), (864, 481), (0, 480), (2050, 480), (864, 2050), (True, 480)],
)
def test_native_profile_refuses_odd_empty_or_oversized_geometry(width: int, height: int) -> None:
    with pytest.raises(AVReconstructionError, match="native_target_geometry"):
        native_av_target_profile(width, height)


def test_native_rule_identity_is_geometry_free_and_distinct_from_legacy() -> None:
    rule = native_av_target_profile_rule_fingerprint()
    assert rule == native_av_target_profile_rule_fingerprint()
    assert native_av_target_profile(864, 480).fingerprint != (
        native_av_target_profile(768, 768).fingerprint
    )
    native = build_production_assembly_capability(
        store_identity_fingerprint=STORE, output_profile_id=M26_NATIVE_OUTPUT_PROFILE_ID
    )
    assert native.output_profile_fingerprint == m26_output_profile_fingerprint(
        M26_NATIVE_OUTPUT_PROFILE_ID
    )
    assert native.output_profile_fingerprint != m26_output_profile_fingerprint(
        M26_OUTPUT_PROFILE_ID
    )
    assert native.runtime_capability_fingerprint == LEGACY_CAPABILITY
    assert native.fingerprint != LEGACY_ASSEMBLY_CAPABILITY


def _native_media(segment_id: str, derived: str, *, fps: int = 24) -> AVMediaDescriptor:
    samples = 32_000
    return AVMediaDescriptor(
        segment_id=segment_id,
        artifact_receipt_fingerprint=derived,
        artifact_output_fingerprint=_fp("output." + segment_id),
        artifact_byte_length=4_096,
        capability_fingerprint=qualified_ffmpeg_capability().fingerprint,
        container="mp4",
        stream_count=2,
        video=AVVideoDescriptor(
            codec="h264",
            width=864,
            height=480,
            pixel_format="yuv420p",
            color_range="tv",
            color_space="bt709",
            color_primaries="bt709",
            color_transfer="bt709",
            rotation_degrees=0,
            frame_rate=AVRational(fps, 1),
            time_base=AVRational(1, 90_000),
            start_time=AVRational(0, 1),
            end_time=AVRational(1, 1),
            decoded_frame_count=fps,
        ),
        audio=AVAudioDescriptor(
            codec="aac",
            sample_format="fltp",
            sample_rate=samples,
            channels=2,
            channel_layout="stereo",
            time_base=AVRational(1, samples),
            start_time=AVRational(0, 1),
            end_time=AVRational(1, 1),
            decoded_sample_count=samples,
        ),
        subtitle_stream_count=0,
        data_stream_count=0,
        attachment_stream_count=0,
        inspected_at_ms=300,
        expires_at_ms=60_300,
        warning_codes=(),
    )


def _native_plan(
    target: AVTargetProfile, *, second_fps: int = 24
) -> tuple[AVReconstructionPlan, object]:
    ids = ("segment.native.1", "segment.native.2")
    derived = (_fp("derived.1"), _fp("derived.2"))
    workspace = _fp("workspace")
    plan = build_m26_derived_av_reconstruction_plan(
        workspace_id="workspace.native",
        workspace_revision=1,
        workspace_fingerprint=workspace,
        assembly_authorization_fingerprint=_fp("authorization"),
        generation_plan_fingerprint=_fp("generation.plan"),
        generation_state_fingerprint=_fp("generation.state"),
        manifest_fingerprints=(_fp("manifest.1"), _fp("manifest.2")),
        derived_input_receipt_fingerprints=derived,
        source_contribution_fingerprints=(_fp("source.1"), _fp("source.2")),
        segment_dispositions=(SelectiveRerunDisposition.QUEUE,) * 2,
        media_descriptors=(
            _native_media(ids[0], derived[0]),
            _native_media(ids[1], derived[1], fps=second_fps),
        ),
        boundary_evidence=(
            AVBoundaryEvidence(
                workspace_id="workspace.native",
                workspace_revision=1,
                workspace_fingerprint=workspace,
                predecessor_segment_id=ids[0],
                successor_segment_id=ids[1],
                receipt=ContinuityBoundaryReceipt(
                    boundary_id="m26.cut.native",
                    mode=ContinuityMode.CUT,
                    audio_not_carried=True,
                ),
            ),
        ),
        capability=qualified_ffmpeg_capability(),
        limits=qualified_av_limits(),
        target_profile=target,
        planned_at_ms=300,
    )
    return plan, approve_av_reconstruction_plan(plan, approved_at_ms=310, expires_at_ms=600)


def test_native_plan_passes_matching_segments_through_and_stays_cut_only() -> None:
    plan, approval = _native_plan(native_av_target_profile(864, 480))
    assert all(item.operations == (AVOperation.PASSTHROUGH,) for item in plan.segments)
    # The seam policy's overlap grid is the legacy 30 fps grid; a native plan never gets seams.
    with pytest.raises(AVSeamPolicyError, match="seam_target_profile_unsupported"):
        build_av_seam_plan(
            base_plan=plan,
            base_approval=approval,  # type: ignore[arg-type]
            seam_specs=(),
            planned_at_ms=320,
        )


def test_native_plan_refuses_a_segment_off_the_native_rates() -> None:
    with pytest.raises(AVReconstructionError, match="m26_derived_input_mismatch"):
        _native_plan(native_av_target_profile(864, 480), second_fps=30)


@pytest.mark.parametrize(
    "target",
    [
        replace(native_av_target_profile(864, 480), sample_rate=48_000),
        replace(native_av_target_profile(864, 480), frame_rate=AVRational(30, 1)),
        replace(native_av_target_profile(864, 480), color_transfer="iec61966-2-1"),
        replace(qualified_av_target_profile(), width=864, height=480),
    ],
    ids=["native_48k", "native_30fps", "native_srgb", "legacy_resized"],
)
def test_only_minted_targets_are_admitted(target: AVTargetProfile) -> None:
    with pytest.raises(AVReconstructionError, match="adapter_capability_changed"):
        _native_plan(target)
