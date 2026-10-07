from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from comfyui_h3_context.adapters import authoring_render_source
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.composition_contract import (
    NLE_OPERATION_IDS,
    PublicAsset,
    PublicCompositionSnapshot,
    Rational,
    TimingLandmark,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.render_planner import (
    FONT_FACTS_SCHEMA,
    PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
    RENDER_PLAN_SCHEMA,
    RENDER_PRIMITIVE_IDS,
    RENDERER_QUALIFICATION_ROW_IDS,
    SOURCE_CURRENTNESS_SCHEMA,
    FontTitleBindingFact,
    PackagedFontFacts,
    PrivateSourceFact,
    PrivateSourceFactsManifest,
    RenderPlannerError,
    SourceCurrentnessConfirmation,
    SourceCurrentnessToken,
    packaged_font_facts_fingerprint,
    plan_render,
    private_source_manifest_fingerprint,
    required_unqualified_renderer_profile,
    revalidate_render_plan_currentness,
    source_facts_fingerprint,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"
TEST_FP = "sha256:" + "0" * 64
FONT_MANIFEST_FINGERPRINT = (
    "sha256:ec4a9cdbdf98c523a03fe3b06cef6f6ef08f5792993ed9469505750e835205aa"
)
FONT_PACKAGE_FINGERPRINT = "sha256:ff39341b729a20b6bb75022dd96401cda5a613db118b858603a60e238cd584c7"
FONT_LICENSE_FINGERPRINT = "sha256:cee9892f9f0cc8fe882c9e9537ee6a89621d86ee7ceaf70b02e2b2b1c25c061a"


def _snapshot_wire() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    wire = cast(dict[str, Any], copy.deepcopy(document["snapshot"]))
    cast(dict[str, Any], wire["assets"][3])["asset_id"] = "h3.font.noto_sans.v1"
    cast(dict[str, Any], wire["clips"][3])["text"]["font_asset_id"] = "h3.font.noto_sans.v1"
    primary = cast(dict[str, Any], wire["assets"][0])
    primary["source_frame_count"] = 48
    primary["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(48)
    ]
    overlay = cast(dict[str, Any], wire["assets"][1])
    # This is an explicit synthetic silent-source fixture, not migration of the historical wire.
    overlay["embedded_audio"] = "absent"
    overlay["source_sample_count"] = None
    overlay["source_frame_count"] = 12
    overlay["landmarks"] = [
        {"frame_index": frame, "pts": frame * 512, "dts": frame * 512, "duration_ticks": 512}
        for frame in range(12)
    ]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


def _snapshot() -> PublicCompositionSnapshot:
    return decode_public_snapshot(_snapshot_wire())


def _enabled_source_fact(
    *, asset_id: str, origin: str, width: int, height: int, duration_ms: int | None
) -> PrivateSourceFact:
    source_profile_fingerprint = canonical_fingerprint(
        {"asset_id": asset_id, "profile": "measured_source_profile_v1"}
    )
    color_facts_fingerprint = canonical_fingerprint(
        {"asset_id": asset_id, "color": "measured_color_facts_v1"}
    )
    size_bytes = 1_000_000 if origin == "context_video" else 32_768
    facts_fingerprint = source_facts_fingerprint(
        asset_id=asset_id,
        source_id=asset_id,
        origin=origin,
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_milliseconds=duration_ms,
        source_profile_fingerprint=source_profile_fingerprint,
        color_facts_fingerprint=color_facts_fingerprint,
    )
    return PrivateSourceFact(
        asset_id=asset_id,
        source_id=asset_id,
        origin=origin,
        disposition="enabled",
        generation=3,
        source_fingerprint=canonical_fingerprint({"asset_id": asset_id, "bytes": "opaque"}),
        source_facts_fingerprint=facts_fingerprint,
        source_profile_fingerprint=source_profile_fingerprint,
        color_facts_fingerprint=color_facts_fingerprint,
        currentness_token=f"current:{asset_id}:g3",
        lease_fingerprint=canonical_fingerprint({"asset_id": asset_id, "lease": 3}),
        width=width,
        height=height,
        size_bytes=size_bytes,
        duration_milliseconds=duration_ms,
    )


def _source_manifest(snapshot: PublicCompositionSnapshot) -> PrivateSourceFactsManifest:
    facts = (
        _enabled_source_fact(
            asset_id="img-overlay", origin="runtime_image", width=320, height=180, duration_ms=None
        ),
        _enabled_source_fact(
            asset_id="vid-overlay", origin="context_video", width=320, height=180, duration_ms=500
        ),
        _enabled_source_fact(
            asset_id="vid-primary", origin="context_video", width=320, height=180, duration_ms=2_000
        ),
    )
    provisional = PrivateSourceFactsManifest(
        schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=3,
        facts=facts,
        manifest_fingerprint="sha256:" + "0" * 64,
    )
    return replace(
        provisional,
        manifest_fingerprint=private_source_manifest_fingerprint(provisional),
    )


def _currentness(manifest: PrivateSourceFactsManifest) -> SourceCurrentnessConfirmation:
    return SourceCurrentnessConfirmation(
        schema_version=SOURCE_CURRENTNESS_SCHEMA,
        manifest_fingerprint=manifest.manifest_fingerprint,
        workspace_handle=manifest.workspace_handle,
        workspace_revision=manifest.workspace_revision,
        timeline_revision=manifest.timeline_revision,
        public_fingerprint=manifest.public_fingerprint,
        generation=manifest.generation,
        tokens=tuple(
            SourceCurrentnessToken(
                asset_id=fact.asset_id,
                source_fingerprint=cast(str, fact.source_fingerprint),
                source_facts_fingerprint=cast(str, fact.source_facts_fingerprint),
                currentness_token=cast(str, fact.currentness_token),
                lease_fingerprint=cast(str, fact.lease_fingerprint),
            )
            for fact in manifest.facts
            if fact.disposition == "enabled"
        ),
    )


def _font_facts(snapshot: PublicCompositionSnapshot) -> PackagedFontFacts:
    title = next(clip for clip in snapshot.clips if clip.text is not None)
    assert title.text is not None
    bindings = (
        FontTitleBindingFact(
            clip_id=title.clip_id,
            font_asset_id=title.text.font_asset_id,
            artifact_id="noto-sans-bold-normal",
            weight=title.text.weight,
            style=title.text.style,
            text_fingerprint=canonical_fingerprint({"content": title.text.content}),
            file_fingerprint=canonical_fingerprint({"font": "bold-normal"}),
            cmap_fingerprint=canonical_fingerprint({"cmap": "latin-v1"}),
            build_version="noto-sans-2.015",
        ),
    )
    provisional = PackagedFontFacts(
        schema_version=FONT_FACTS_SCHEMA,
        manifest_schema_version="h3.authoring.packaged_font_manifest.v1",
        profile_id="h3.authoring.font_profile.v1",
        fallback_order=("h3.font.noto_sans.v1",),
        license_spdx_id="OFL-1.1",
        manifest_fingerprint=FONT_MANIFEST_FINGERPRINT,
        package_fingerprint=FONT_PACKAGE_FINGERPRINT,
        license_file_fingerprint=FONT_LICENSE_FINGERPRINT,
        title_bindings=bindings,
        facts_fingerprint="sha256:" + "0" * 64,
    )
    return replace(provisional, facts_fingerprint=packaged_font_facts_fingerprint(provisional))


def _inputs() -> tuple[
    PublicCompositionSnapshot,
    PrivateSourceFactsManifest,
    SourceCurrentnessConfirmation,
    PackagedFontFacts,
]:
    snapshot = _snapshot()
    manifest = _source_manifest(snapshot)
    return snapshot, manifest, _currentness(manifest), _font_facts(snapshot)


def _error_code(call: Any) -> str:
    with pytest.raises(RenderPlannerError) as exc_info:
        call()
    return exc_info.value.code


def test_legacy_policy_label_is_not_measured_source_authority_for_new_plan() -> None:
    wire = _snapshot_wire()
    wire["assets"][1]["embedded_audio"] = "excluded_overlay_policy"
    wire["assets"][1]["source_sample_count"] = None
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    before = copy.deepcopy(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=_currentness(manifest),
                font_facts=_font_facts(snapshot),
            )
        )
        == "source_unavailable"
    )
    assert snapshot.to_wire() == before


def test_profile_is_closed_exact_and_unqualified_without_fictitious_renderer_facts() -> None:
    profile = required_unqualified_renderer_profile()

    assert profile.qualification == "unqualified"
    assert profile.execution_capability == "unavailable"
    assert (
        profile.renderer_id,
        profile.renderer_version,
        profile.renderer_build_fingerprint,
        profile.renderer_artifact_fingerprint,
        profile.renderer_license_spdx_id,
    ) == (None, None, None, None, None)
    assert profile.semantic_command_ids == NLE_OPERATION_IDS
    assert len(profile.semantic_command_ids) == 34  # with `set_clip_audio`
    assert profile.render_primitive_ids == RENDER_PRIMITIVE_IDS
    assert tuple(row.row_id for row in profile.qualification_rows) == (
        RENDERER_QUALIFICATION_ROW_IDS
    )
    assert {row.disposition for row in profile.qualification_rows} == {"unqualified"}
    assert {row.evidence_fingerprint for row in profile.qualification_rows} == {None}
    assert profile.output.container == "mp4"
    assert profile.output.video_codec == "h264"
    assert profile.output.pixel_format == "yuv420p"
    assert profile.output.color_policy == "bt709_sdr_limited_v1"
    assert profile.output.audio_policy == "primary_embedded_follow_video_v1"
    assert profile.audio_extension.command_members == ()
    assert profile.audio_extension.independent_audio_renderer_variant == "none_v1"


def test_plan_is_deterministic_content_free_and_uses_only_resolved_render_primitives() -> None:
    snapshot, manifest, currentness, fonts = _inputs()

    first = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=fonts,
    )
    second = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=fonts,
    )

    assert first == second
    assert first.schema_version == RENDER_PLAN_SCHEMA
    assert first.snapshot_fingerprint == snapshot.public_fingerprint
    assert first.source_manifest_fingerprint == manifest.manifest_fingerprint
    assert first.font_manifest_fingerprint == FONT_MANIFEST_FINGERPRINT
    assert first.renderer_capability_profile_fingerprint == (
        required_unqualified_renderer_profile().profile_fingerprint
    )
    assert first.unavailable_disposition == "renderer_unqualified"
    assert first.output_profile == snapshot.output
    assert sum(len(chunk.frames) for chunk in first.resolved_operations) == 48

    frame_12 = first.resolved_operations[0].frames[12]
    assert frame_12.layers[0].source_pts == 6_144
    assert frame_12.audio_span is not None
    assert frame_12.audio_span.clip_id == "clip-main"
    assert frame_12.audio_operation_id == "PrimaryEmbeddedFollowVideoV1"
    assert frame_12.layers[1].operation_ids == (
        "SelectSourceRangeV1",
        "CropV1",
        "Transform2DV1",
        "ColorAdjustV1",
        "OpacityV1",
        "BlendV1",
        "CrossDissolveV1",
    )
    admitted = set(RENDER_PRIMITIVE_IDS)
    assert {
        operation_id
        for chunk in first.resolved_operations
        for frame in chunk.frames
        for layer in frame.layers
        for operation_id in layer.operation_ids
    } <= admitted
    assert not set(NLE_OPERATION_IDS) & admitted

    wire_text = json.dumps(first.to_wire(), sort_keys=True)
    for private_field in (
        "source_path",
        "source_url",
        "runtime_identity",
        "device_handle",
        "credential",
        "signed_url",
    ):
        assert private_field not in wire_text


def test_source_manifest_requires_exact_origin_facts_fingerprint_and_fresh_confirmation() -> None:
    snapshot, manifest, currentness, fonts = _inputs()
    image_index = next(
        index for index, fact in enumerate(manifest.facts) if fact.asset_id == "img-overlay"
    )
    wrong_image = replace(manifest.facts[image_index], origin="context_video")
    wrong_facts = list(manifest.facts)
    wrong_facts[image_index] = wrong_image
    wrong_origin = replace(manifest, facts=tuple(wrong_facts))
    wrong_origin = replace(
        wrong_origin,
        manifest_fingerprint=private_source_manifest_fingerprint(wrong_origin),
    )
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=wrong_origin,
                currentness=_currentness(wrong_origin),
                font_facts=fonts,
            )
        )
        == "source_origin_mismatch"
    )

    facts_drift = replace(manifest.facts[0], width=321)
    drifted = replace(manifest, facts=(facts_drift, *manifest.facts[1:]))
    drifted = replace(drifted, manifest_fingerprint=private_source_manifest_fingerprint(drifted))
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=drifted,
                currentness=_currentness(drifted),
                font_facts=fonts,
            )
        )
        == "source_facts_mismatch"
    )

    stale = replace(currentness, generation=4)
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=stale,
                font_facts=fonts,
            )
        )
        == "source_replaced"
    )


def test_generated_source_stays_unregistered_and_cannot_satisfy_an_active_clip() -> None:
    snapshot, manifest, currentness, fonts = _inputs()
    image_index = next(
        index for index, fact in enumerate(manifest.facts) if fact.asset_id == "img-overlay"
    )
    generated = PrivateSourceFact(
        asset_id="img-overlay",
        source_id="img-overlay",
        origin="generated",
        disposition="unregistered",
        generation=3,
        source_fingerprint=None,
        source_facts_fingerprint=None,
        source_profile_fingerprint=None,
        color_facts_fingerprint=None,
        currentness_token=None,
        lease_fingerprint=None,
        width=None,
        height=None,
        size_bytes=0,
        duration_milliseconds=None,
    )
    facts = list(manifest.facts)
    facts[image_index] = generated
    unavailable = replace(manifest, facts=tuple(facts))
    unavailable = replace(
        unavailable,
        manifest_fingerprint=private_source_manifest_fingerprint(unavailable),
    )
    confirmation = _currentness(unavailable)

    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=unavailable,
                currentness=confirmation,
                font_facts=fonts,
            )
        )
        == "source_unavailable"
    )


def test_font_title_binding_matches_packaged_fallback_style_and_exact_text() -> None:
    snapshot, manifest, currentness, fonts = _inputs()

    missing = replace(fonts, title_bindings=())
    missing = replace(missing, facts_fingerprint=packaged_font_facts_fingerprint(missing))
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=currentness,
                font_facts=missing,
            )
        )
        == "font_glyph_unsupported"
    )

    binding = fonts.title_bindings[0]
    wrong_text = replace(binding, text_fingerprint=canonical_fingerprint({"content": "different"}))
    drifted = replace(fonts, title_bindings=(wrong_text,))
    drifted = replace(drifted, facts_fingerprint=packaged_font_facts_fingerprint(drifted))
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=currentness,
                font_facts=drifted,
            )
        )
        == "font_glyph_unsupported"
    )


def test_audio_seam_and_renderer_qualification_fail_closed_before_plan_creation() -> None:
    snapshot, manifest, currentness, fonts = _inputs()
    audio_drift = replace(snapshot.audio_extension, command_members=("gain",))
    invalid_audio = replace(snapshot, audio_extension=audio_drift)
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=invalid_audio,
                source_manifest=manifest,
                currentness=currentness,
                font_facts=fonts,
            )
        )
        == "audio_editing_deferred"
    )

    profile = replace(
        required_unqualified_renderer_profile(),
        qualification="qualified",
        execution_capability="available",
    )
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=currentness,
                font_facts=fonts,
                renderer_profile=profile,
            )
        )
        == "renderer_profile_unqualified"
    )


def test_late_source_replacement_is_detected_by_explicit_currentness_handoff() -> None:
    snapshot, manifest, currentness, fonts = _inputs()
    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=fonts,
    )
    revalidate_render_plan_currentness(plan=plan, confirmation=currentness)

    first = currentness.tokens[0]
    replacement = replace(
        first,
        source_fingerprint=canonical_fingerprint({"asset_id": first.asset_id, "bytes": "new"}),
    )
    changed = replace(currentness, tokens=(replacement, *currentness.tokens[1:]))
    assert (
        _error_code(lambda: revalidate_render_plan_currentness(plan=plan, confirmation=changed))
        == "source_replaced"
    )


def test_planner_requires_complete_bounded_video_landmarks_and_uses_vfr_pts() -> None:
    wire = _snapshot_wire()
    primary = cast(dict[str, Any], wire["assets"][0])
    primary["source_time_base"] = {"num": 1, "den": 1_000}
    primary["source_frame_count"] = 6
    primary["landmarks"] = [
        {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 40},
        {"frame_index": 1, "pts": 40, "dts": 40, "duration_ticks": 80},
        {"frame_index": 2, "pts": 120, "dts": 120, "duration_ticks": 35},
        {"frame_index": 3, "pts": 155, "dts": 155, "duration_ticks": 60},
        {"frame_index": 4, "pts": 215, "dts": 215, "duration_ticks": 45},
        {"frame_index": 5, "pts": 260, "dts": 260, "duration_ticks": 40},
    ]
    primary_clip = cast(dict[str, Any], wire["clips"][0])
    primary_clip["source_start_frame"] = 1
    primary_clip["duration_frames"] = 6
    wire["clips"] = [primary_clip]
    cast(dict[str, Any], wire["output"])["duration_frames"] = 6
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    currentness = _currentness(manifest)
    fonts = _font_facts(_snapshot())
    fonts = replace(fonts, title_bindings=())
    fonts = replace(fonts, facts_fingerprint=packaged_font_facts_fingerprint(fonts))

    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=fonts,
    )
    assert [frame.layers[0].source_pts for frame in plan.resolved_operations[0].frames] == [
        40,
        40,
        120,
        155,
        155,
        215,
    ]

    sparse_primary = copy.deepcopy(primary)
    sparse_primary["landmarks"] = sparse_primary["landmarks"][::2]
    wire["assets"][0] = sparse_primary
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    sparse = decode_public_snapshot(wire)
    sparse_manifest = _source_manifest(sparse)
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=sparse,
                source_manifest=sparse_manifest,
                currentness=_currentness(sparse_manifest),
                font_facts=fonts,
            )
        )
        == "source_range_unavailable"
    )


def test_required_limits_cover_the_accepted_3600_frame_timeline_extent() -> None:
    snapshot, manifest, currentness, fonts = _inputs()
    expanded = replace(snapshot.output, duration_frames=3_600)
    wire = snapshot.to_wire()
    wire["output"] = expanded.to_wire()
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    long_snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(long_snapshot)
    currentness = _currentness(manifest)

    plan = plan_render(
        snapshot=long_snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=fonts,
    )
    assert plan.limits.max_plan_frames >= 3_600
    assert sum(len(chunk.frames) for chunk in plan.resolved_operations) == 3_600


def test_planner_uses_output_time_for_complete_low_rate_source() -> None:
    wire = _snapshot_wire()
    primary = cast(dict[str, Any], wire["assets"][0])
    primary["source_time_base"] = {"num": 1, "den": 1_200}
    primary["source_frame_count"] = 24
    primary["landmarks"] = [
        {"frame_index": frame, "pts": frame * 100, "dts": frame * 100, "duration_ticks": 100}
        for frame in range(24)
    ]
    primary_clip = cast(dict[str, Any], wire["clips"][0])
    wire["clips"] = [primary_clip]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    fonts = _font_facts(_snapshot())
    fonts = replace(fonts, title_bindings=())
    fonts = replace(fonts, facts_fingerprint=packaged_font_facts_fingerprint(fonts))

    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=_currentness(manifest),
        font_facts=fonts,
    )
    assert plan.resolved_operations[0].frames[-1].layers[0].source_frame == 23


def test_planner_rejects_true_high_rate_end_overrun() -> None:
    wire = _snapshot_wire()
    primary = cast(dict[str, Any], wire["assets"][0])
    primary["source_time_base"] = {"num": 1, "den": 6_000}
    primary["source_frame_count"] = 60
    primary["landmarks"] = [
        {"frame_index": frame, "pts": frame * 100, "dts": frame * 100, "duration_ticks": 100}
        for frame in range(60)
    ]
    primary_clip = cast(dict[str, Any], wire["clips"][0])
    primary_clip["source_start_frame"] = 48
    primary_clip["duration_frames"] = 12
    wire["clips"] = [primary_clip]
    cast(dict[str, Any], wire["output"])["duration_frames"] = 12
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    manifest = _source_manifest(snapshot)
    fonts = _font_facts(_snapshot())
    fonts = replace(fonts, title_bindings=())
    fonts = replace(fonts, facts_fingerprint=packaged_font_facts_fingerprint(fonts))

    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=manifest,
                currentness=_currentness(manifest),
                font_facts=fonts,
            )
        )
        == "source_range_unavailable"
    )


def test_title_only_plan_accepts_empty_generation_zero_source_facts_and_emits_silence() -> None:
    wire = _snapshot_wire()
    wire["assets"] = [wire["assets"][3]]
    wire["tracks"] = [wire["tracks"][0], wire["tracks"][3]]
    wire["tracks"][1]["order"] = 1
    wire["clips"] = [wire["clips"][3]]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    provisional = PrivateSourceFactsManifest(
        schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=0,
        facts=(),
        manifest_fingerprint="sha256:" + "0" * 64,
    )
    manifest = replace(
        provisional,
        manifest_fingerprint=private_source_manifest_fingerprint(provisional),
    )
    currentness = SourceCurrentnessConfirmation(
        schema_version=SOURCE_CURRENTNESS_SCHEMA,
        manifest_fingerprint=manifest.manifest_fingerprint,
        workspace_handle=manifest.workspace_handle,
        workspace_revision=manifest.workspace_revision,
        timeline_revision=manifest.timeline_revision,
        public_fingerprint=manifest.public_fingerprint,
        generation=0,
        tokens=(),
    )

    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=currentness,
        font_facts=_font_facts(snapshot),
    )
    assert plan.source_bindings == ()
    assert plan.emits_audio_stream is False
    title_frame = plan.resolved_operations[0].frames[6]
    assert title_frame.audio_span is None
    assert title_frame.audio_operation_id is None
    assert title_frame.layers[0].operation_ids[-1] == "DrawTextV1"

    invalid_generation = replace(manifest, generation=1)
    invalid_generation = replace(
        invalid_generation,
        manifest_fingerprint=private_source_manifest_fingerprint(invalid_generation),
    )
    assert (
        _error_code(
            lambda: plan_render(
                snapshot=snapshot,
                source_manifest=invalid_generation,
                currentness=replace(
                    currentness,
                    manifest_fingerprint=invalid_generation.manifest_fingerprint,
                    generation=1,
                ),
                font_facts=_font_facts(snapshot),
            )
        )
        == "source_replaced"
    )


def test_authoring_capacity_does_not_become_a_96_frame_render_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_asset = PublicAsset(
        "vid-primary",
        "video",
        Rational(1, 12_288),
        96,
        None,
        "absent",
        "nonnegative_monotonic_v1",
        tuple(TimingLandmark(frame, frame * 512, frame * 512, 512) for frame in range(96)),
    )
    source_claim = SimpleNamespace(asset=source_asset, current=lambda: True)
    monkeypatch.setattr(
        authoring_render_source,
        "claim_render_source",
        lambda _receipt, _source_id: source_claim,
    )
    projection: dict[str, object] = {
        "schema": "h3.context.authoring_workbench.projection.v1",
        "workspace_handle": "authoring.content-extent-fixture",
        "context_source_id": "context.content-extent-fixture",
        "task_mode": "T2VA",
        "registry_fingerprint": TEST_FP,
        "reference": {
            "revision": 1,
            "sources": [{"source_id": "vid-primary", "kind": "video", "admitted": True}],
        },
        "availability": {"producer": "fixture", "revision": 1},
        "timeline": {
            "revision": 1,
            "content_fingerprint": TEST_FP,
            "profile": {"max_extent_frames": 3_600},
            "clips": [
                {
                    "clip_id": "clip-96",
                    "asset_id": "vid-primary",
                    "kind": "video",
                    "lane": 0,
                    "start_frame": 0,
                    "frames": 96,
                    "source_start_frame": 0,
                    "envelope": [],
                }
            ],
            "links": [],
            "selection": [],
            "blockers": [],
        },
        "rejection": None,
    }
    prepared = authoring_render_source.prepare_authoring_history(
        projection,
        SimpleNamespace(generation=3),  # type: ignore[arg-type]
    )
    snapshot = prepared.snapshot
    assert snapshot is not None
    source_fact = _enabled_source_fact(
        asset_id="vid-primary",
        origin="context_video",
        width=320,
        height=180,
        duration_ms=4_000,
    )
    provisional_manifest = PrivateSourceFactsManifest(
        schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        public_fingerprint=snapshot.public_fingerprint,
        generation=3,
        facts=(source_fact,),
        manifest_fingerprint=TEST_FP,
    )
    manifest = replace(
        provisional_manifest,
        manifest_fingerprint=private_source_manifest_fingerprint(provisional_manifest),
    )
    fonts = _font_facts(_snapshot())
    fonts = replace(fonts, title_bindings=())
    fonts = replace(fonts, facts_fingerprint=packaged_font_facts_fingerprint(fonts))

    plan = plan_render(
        snapshot=snapshot,
        source_manifest=manifest,
        currentness=_currentness(manifest),
        font_facts=fonts,
    )

    # Independent fixture facts: one placed 96-frame clip, 24 fps, and a 64-frame chunk cap.
    assert sum(len(chunk.frames) for chunk in plan.resolved_operations) == 96
    assert len(plan.resolved_operations) == 2
