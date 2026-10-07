from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    AUDIO_EXTENSION_SCHEMA,
    BLOCKER_CODES,
    INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    NLE_OPERATION_IDS,
    OUTPUT_PROFILE_ID,
    PRIVATE_SOURCE_MANIFEST_SCHEMA,
    PUBLIC_SNAPSHOT_SCHEMA,
    RENDER_JOB_STATES,
    RENDER_TERMINAL_REASONS,
    CompositionContractError,
    DerivativeManifest,
    PrivateSourceManifest,
    adapt_authoring_projection,
    decode_public_snapshot,
    decode_public_snapshot_json,
    operation_disposition,
    public_snapshot_fingerprint,
    resolve_composition,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m25_10_composition_contract_v1.json"


def fixture_document() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def fixture_snapshot() -> dict[str, Any]:
    return cast(dict[str, Any], fixture_document()["snapshot"])


def resign(wire: dict[str, Any]) -> dict[str, Any]:
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    return wire


def rejection_code(callable_: Callable[[], object]) -> str:
    with pytest.raises(CompositionContractError) as exc_info:
        callable_()
    return exc_info.value.code


def test_closed_profiles_and_machine_enumerable_vocabularies() -> None:
    assert PUBLIC_SNAPSHOT_SCHEMA == "h3.context.public_composition_snapshot.v1"
    assert OUTPUT_PROFILE_ID == "h3.authoring.output.h264_aac_24fps.v1"
    assert AUDIO_EXTENSION_SCHEMA == "h3.authoring.independent_audio_extension.v1"
    assert INDEPENDENT_AUDIO_COMMAND_NAMESPACE == "h3.authoring.audio.command.v1"
    # 33 accepted operations and `set_clip_audio`, a video clip's own gain, mute and fades.
    assert len(NLE_OPERATION_IDS) == 34
    assert len(NLE_OPERATION_IDS) == len(set(NLE_OPERATION_IDS))
    assert RENDER_JOB_STATES == (
        "queued",
        "probing",
        "preparing",
        "rendering",
        "encoding",
        "muxing",
        "validating",
        "succeeded",
        "failed",
        "cancelled",
    )
    assert RENDER_TERMINAL_REASONS[-3:] == (
        "renderer_failed",
        "validation_failed",
        "internal_failure",
    )
    assert {"private_field", "invalid_timing", "audio_editing_deferred"} <= set(BLOCKER_CODES)


def test_v1_snapshot_reader_refuses_the_versioned_nle_authoring_schema() -> None:
    wire = fixture_snapshot()
    wire["schema"] = "h3.context.nle_authoring_state.v1"

    assert rejection_code(lambda: decode_public_snapshot(wire)) == "unsupported_profile"


def test_shared_fixture_round_trips_and_fingerprint_is_exact() -> None:
    wire = fixture_snapshot()
    snapshot = decode_public_snapshot(wire)
    assert snapshot.to_wire() == wire
    assert public_snapshot_fingerprint(wire) == wire["public_fingerprint"]
    assert decode_public_snapshot_json(json.dumps(wire, ensure_ascii=False)).to_wire() == wire


@pytest.mark.parametrize("disposition", ["present_bound", "absent", "unavailable"])
def test_overlay_admits_measured_audio_facts_without_relabelling(disposition: str) -> None:
    wire = fixture_snapshot()
    overlay = wire["assets"][1]
    overlay["embedded_audio"] = disposition
    overlay["source_sample_count"] = 48_000 if disposition == "present_bound" else None
    resign(wire)
    before = json.dumps(wire, sort_keys=True)
    snapshot = decode_public_snapshot(wire)
    assert snapshot.to_wire() == wire
    assert snapshot.assets[1].to_wire() == overlay
    expected_fingerprints = {
        "present_bound": "sha256:779a7a086b4f0d75dad978060de5d839bd5ad4aa7a95596e40530163bd161793",
        "absent": "sha256:50f5a4630528f85d2d76fbcbcfc27bdb4faa461eb21e2f07245980f419e9e041",
        "unavailable": "sha256:f5b5151347929e2d128ab696b158270efce18a9de9f5ca34bd4406cc7227b227",
    }
    assert snapshot.public_fingerprint == expected_fingerprints[disposition]
    scene = resolve_composition(snapshot, 12)
    assert any(layer.asset_id == "vid-overlay" for layer in scene.layers)
    assert scene.audio_span is not None and scene.audio_span.asset_id == "vid-primary"
    assert not scene.blockers
    assert json.dumps(wire, sort_keys=True) == before


@pytest.mark.parametrize("primary_enabled", [True, False])
def test_same_source_in_primary_and_overlay_has_only_primary_audio(primary_enabled: bool) -> None:
    wire = fixture_snapshot()
    wire["clips"][1]["asset_id"] = "vid-primary"
    wire["clips"][1]["transition"] = {"kind": "none", "duration_frames": 0}
    wire["tracks"][0]["enabled"] = primary_enabled
    snapshot = decode_public_snapshot(resign(wire))
    scene = resolve_composition(snapshot, 12)
    video_layers = [layer for layer in scene.layers if layer.asset_id == "vid-primary"]
    assert len(video_layers) == (2 if primary_enabled else 1)
    assert (scene.audio_span is not None) == primary_enabled
    if scene.audio_span is not None:
        assert scene.audio_span.clip_id == "clip-main"
    assert snapshot.assets[0].to_wire() == wire["assets"][0]


def test_legacy_overlay_remains_read_only_decodable_with_unchanged_fingerprint() -> None:
    wire = fixture_snapshot()
    assert wire["assets"][1]["embedded_audio"] == "excluded_overlay_policy"
    before = json.dumps(wire, sort_keys=True)
    assert decode_public_snapshot(wire).to_wire() == wire
    assert public_snapshot_fingerprint(wire) == wire["public_fingerprint"]
    assert json.dumps(wire, sort_keys=True) == before


def test_source_timebase_admits_the_exact_m25_09_cfr_clock() -> None:
    wire = fixture_snapshot()
    asset = wire["assets"][0]
    asset["source_time_base"] = {"num": 1, "den": 12_288}
    for landmark in asset["landmarks"]:
        landmark["pts"] = landmark["frame_index"] * 512
        landmark["dts"] = landmark["frame_index"] * 512
        landmark["duration_ticks"] = 512

    snapshot = decode_public_snapshot(resign(wire))
    assert snapshot.assets[0].source_time_base is not None
    assert snapshot.assets[0].source_time_base.to_wire() == {"num": 1, "den": 12_288}
    scene = resolve_composition(snapshot, 12)
    assert scene.layers[0].source_pts == 6_144
    assert scene.audio_span is not None
    assert (scene.audio_span.source_start_sample, scene.audio_span.source_end_sample) == (
        24_000,
        26_000,
    )

    output_drift = fixture_snapshot()
    output_drift["output"]["time_base"] = {"num": 1, "den": 12_288}
    assert (
        rejection_code(lambda: decode_public_snapshot(resign(output_drift))) == "invalid_contract"
    )

    unsafe_source = fixture_snapshot()
    unsafe_source["assets"][0]["source_time_base"] = {
        "num": 1,
        "den": 9_007_199_254_740_992,
    }
    assert rejection_code(lambda: decode_public_snapshot(unsafe_source)) == "invalid_contract"


def test_decoder_rejects_duplicate_unknown_private_and_stale_identity() -> None:
    wire = fixture_snapshot()
    assert (
        rejection_code(lambda: decode_public_snapshot({**wire, "surprise": True}))
        == "invalid_contract"
    )
    assert (
        rejection_code(lambda: decode_public_snapshot({**wire, "source_path": "private.mp4"}))
        == "private_field"
    )
    assert (
        rejection_code(
            lambda: decode_public_snapshot({**wire, "public_fingerprint": "sha256:" + "f" * 64})
        )
        == "stale_snapshot"
    )
    text = json.dumps(wire).replace('"project_id":', '"project_id":"duplicate","project_id":', 1)
    assert rejection_code(lambda: decode_public_snapshot_json(text)) == "invalid_contract"


def test_timing_and_resource_failures_are_closed() -> None:
    wire = fixture_snapshot()
    negative = json.loads(json.dumps(wire))
    negative["assets"][0]["landmarks"][0]["pts"] = -1
    assert rejection_code(lambda: decode_public_snapshot(negative)) == "negative_timestamp"

    nonmonotonic = json.loads(json.dumps(wire))
    nonmonotonic["assets"][0]["landmarks"][1]["pts"] = 0
    assert rejection_code(lambda: decode_public_snapshot(nonmonotonic)) == "invalid_timing"

    oversized = json.loads(json.dumps(wire))
    oversized["tracks"] = oversized["tracks"] * 3
    assert rejection_code(lambda: decode_public_snapshot(oversized)) == "resource_limit"

    outside_source = fixture_snapshot()
    outside_source["assets"][0]["landmarks"][-1]["frame_index"] = 100
    assert (
        rejection_code(lambda: decode_public_snapshot(resign(outside_source))) == "invalid_timing"
    )


def test_capability_profile_exactly_binds_selected_runtime_and_observation_corpus() -> None:
    capability = fixture_snapshot()["capability"]
    assert capability["input_containers"] == ["mp4"]
    assert capability["input_video_codecs"] == ["h264"]
    assert capability["input_audio_codecs"] == ["aac"]
    assert capability["input_pixel_formats"] == ["yuv420p"]
    assert capability["media_transport"] == "intrinsic_html_media_element_v1"
    assert capability["frame_observer"] == "request_video_frame_callback_v1"
    assert capability["frame_event_fallbacks"] == ["seeked", "timeupdate"]
    assert capability["visual_compositor"] == "canvas2d_ladder_v2"
    assert capability["cross_origin_isolation_required"] is False
    assert capability["mse_required"] is False
    assert capability["webcodecs_required"] is False
    assert capability["network_policy"] == "same_origin_bounded_body_only_v1"
    assert capability["observation_corpus_ids"] == [
        "cfr",
        "invalid",
        "lane",
        "mse",
        "truncated",
        "vfr",
    ]
    assert all(
        isinstance(value, str) and value.startswith("sha256:")
        for value in capability["observation_corpus_fingerprints"]
    )
    assert capability["probe_root_keys"] == ["format", "programs", "stream_groups", "streams"]
    assert capability["probe_format_keys"] == ["duration", "format_name"]
    assert capability["probe_video_stream_keys"] == [
        "avg_frame_rate",
        "codec_name",
        "codec_type",
        "height",
        "pix_fmt",
        "width",
    ]
    assert capability["probe_audio_stream_keys"] == [
        "avg_frame_rate",
        "channel_layout",
        "channels",
        "codec_name",
        "codec_type",
        "sample_rate",
    ]
    assert capability["probe_empty_array_keys"] == ["programs", "stream_groups"]
    assert capability["audio_stream_rate_sentinel"] == "0/0"

    for key, value in capability.items():
        drifted = fixture_snapshot()
        if isinstance(value, bool):
            replacement: object = not value
        elif isinstance(value, int):
            replacement = value + 1
        elif isinstance(value, str):
            replacement = value + ".drift"
        else:
            replacement = [*value, "drift"]
        drifted["capability"][key] = replacement
        assert (
            rejection_code(partial(decode_public_snapshot, resign(drifted)))
            == "unsupported_profile"
        ), key


def test_clip_reference_integrity_precedes_transition_traversal() -> None:
    wire = fixture_snapshot()
    wire["tracks"][0]["enabled"] = False
    wire["clips"][-1]["track_id"] = "unknown-track"
    assert rejection_code(lambda: decode_public_snapshot(resign(wire))) == "invalid_contract"


def test_visual_property_matrix_and_transition_participant_fail_closed() -> None:
    mutations: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
        ("anchor_x_min", lambda wire: wire["clips"][0]["transform"].__setitem__("anchor_x_bp", -1)),
        (
            "anchor_x_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("anchor_x_bp", 10_001),
        ),
        ("anchor_y_min", lambda wire: wire["clips"][0]["transform"].__setitem__("anchor_y_bp", -1)),
        (
            "anchor_y_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("anchor_y_bp", 10_001),
        ),
        (
            "position_x_min",
            lambda wire: wire["clips"][0]["transform"].__setitem__("position_x_bp", -40_001),
        ),
        (
            "position_x_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("position_x_bp", 40_001),
        ),
        (
            "position_y_min",
            lambda wire: wire["clips"][0]["transform"].__setitem__("position_y_bp", -40_001),
        ),
        (
            "position_y_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("position_y_bp", 40_001),
        ),
        ("scale_x_min", lambda wire: wire["clips"][0]["transform"].__setitem__("scale_x_bp", 0)),
        (
            "scale_x_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("scale_x_bp", 80_001),
        ),
        ("scale_y_min", lambda wire: wire["clips"][0]["transform"].__setitem__("scale_y_bp", 0)),
        (
            "scale_y_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("scale_y_bp", 80_001),
        ),
        (
            "rotation_min",
            lambda wire: wire["clips"][0]["transform"].__setitem__("rotation_mdeg", -180_001),
        ),
        (
            "rotation_max",
            lambda wire: wire["clips"][0]["transform"].__setitem__("rotation_mdeg", 180_001),
        ),
        ("crop_left_min", lambda wire: wire["clips"][0]["crop"].__setitem__("left_bp", -1)),
        (
            "crop_left_max",
            lambda wire: wire["clips"][0]["crop"].__setitem__("left_bp", 10_000),
        ),
        ("crop_top_min", lambda wire: wire["clips"][0]["crop"].__setitem__("top_bp", -1)),
        (
            "crop_top_max",
            lambda wire: wire["clips"][0]["crop"].__setitem__("top_bp", 10_000),
        ),
        (
            "crop_right_min",
            lambda wire: wire["clips"][0]["crop"].__setitem__("right_bp", -1),
        ),
        (
            "crop_right_max",
            lambda wire: wire["clips"][0]["crop"].__setitem__("right_bp", 10_000),
        ),
        (
            "crop_bottom_min",
            lambda wire: wire["clips"][0]["crop"].__setitem__("bottom_bp", -1),
        ),
        (
            "crop_bottom_max",
            lambda wire: wire["clips"][0]["crop"].__setitem__("bottom_bp", 10_000),
        ),
        (
            "crop_horizontal_sum",
            lambda wire: wire["clips"][0]["crop"].update({"left_bp": 5_000, "right_bp": 5_000}),
        ),
        (
            "crop_vertical_sum",
            lambda wire: wire["clips"][0]["crop"].update({"top_bp": 5_000, "bottom_bp": 5_000}),
        ),
        ("opacity_min", lambda wire: wire["clips"][0].__setitem__("opacity_bp", -1)),
        ("opacity_max", lambda wire: wire["clips"][0].__setitem__("opacity_bp", 10_001)),
        ("blend", lambda wire: wire["clips"][0].__setitem__("blend", "plugin")),
        ("text_empty", lambda wire: wire["clips"][3]["text"].__setitem__("content", "")),
        (
            "text_length",
            lambda wire: wire["clips"][3]["text"].__setitem__("content", "x" * 2_049),
        ),
        (
            "text_lines",
            lambda wire: wire["clips"][3]["text"].__setitem__("content", "\n".join(["x"] * 33)),
        ),
        ("text_control", lambda wire: wire["clips"][3]["text"].__setitem__("content", "x\u0001")),
        (
            "text_font",
            lambda wire: wire["clips"][3]["text"].__setitem__("font_asset_id", "vid-primary"),
        ),
        ("text_size_min", lambda wire: wire["clips"][3]["text"].__setitem__("size_px", 7)),
        ("text_size_max", lambda wire: wire["clips"][3]["text"].__setitem__("size_px", 513)),
        ("text_weight_min", lambda wire: wire["clips"][3]["text"].__setitem__("weight", 399)),
        ("text_weight_gap", lambda wire: wire["clips"][3]["text"].__setitem__("weight", 500)),
        ("text_weight_max", lambda wire: wire["clips"][3]["text"].__setitem__("weight", 701)),
        ("text_style", lambda wire: wire["clips"][3]["text"].__setitem__("style", "oblique")),
        ("text_align", lambda wire: wire["clips"][3]["text"].__setitem__("align", "justify")),
        (
            "text_line_height_min",
            lambda wire: wire["clips"][3]["text"].__setitem__("line_height_bp", 7_499),
        ),
        (
            "text_line_height_max",
            lambda wire: wire["clips"][3]["text"].__setitem__("line_height_bp", 30_001),
        ),
        (
            "text_fill_min",
            lambda wire: wire["clips"][3]["text"].__setitem__("fill_rgba", [-1, 0, 0, 0]),
        ),
        (
            "text_fill_max",
            lambda wire: wire["clips"][3]["text"].__setitem__("fill_rgba", [256, 0, 0, 0]),
        ),
        (
            "text_fill_shape",
            lambda wire: wire["clips"][3]["text"].__setitem__("fill_rgba", [0, 0, 0]),
        ),
        (
            "transition_none_duration",
            lambda wire: wire["clips"][0]["transition"].__setitem__("duration_frames", 1),
        ),
        (
            "transition_cross_min",
            lambda wire: wire["clips"][1]["transition"].__setitem__("duration_frames", 0),
        ),
        (
            "transition_cross_clip_max",
            lambda wire: wire["clips"][1]["transition"].__setitem__("duration_frames", 13),
        ),
        (
            "transition_kind",
            lambda wire: wire["clips"][1]["transition"].__setitem__("kind", "wipe"),
        ),
        (
            "effect_none_brightness",
            lambda wire: wire["clips"][0]["effect"].__setitem__("brightness_permille", 1),
        ),
        (
            "effect_none_contrast",
            lambda wire: wire["clips"][0]["effect"].__setitem__("contrast_permille", 999),
        ),
        (
            "effect_none_saturation",
            lambda wire: wire["clips"][0]["effect"].__setitem__("saturation_permille", 999),
        ),
        (
            "effect_brightness_min",
            lambda wire: wire["clips"][1]["effect"].__setitem__("brightness_permille", -1_001),
        ),
        (
            "effect_brightness_max",
            lambda wire: wire["clips"][1]["effect"].__setitem__("brightness_permille", 1_001),
        ),
        (
            "effect_contrast_min",
            lambda wire: wire["clips"][1]["effect"].__setitem__("contrast_permille", -1),
        ),
        (
            "effect_contrast_max",
            lambda wire: wire["clips"][1]["effect"].__setitem__("contrast_permille", 2_001),
        ),
        (
            "effect_saturation_min",
            lambda wire: wire["clips"][1]["effect"].__setitem__("saturation_permille", -1),
        ),
        (
            "effect_saturation_max",
            lambda wire: wire["clips"][1]["effect"].__setitem__("saturation_permille", 2_001),
        ),
        ("effect_kind", lambda wire: wire["clips"][0]["effect"].__setitem__("kind", "plugin")),
    )
    for name, mutate in mutations:
        wire = fixture_snapshot()
        mutate(wire)
        assert (
            rejection_code(partial(decode_public_snapshot, resign(wire))) == "invalid_contract"
        ), name

    no_lower_participant = fixture_snapshot()
    no_lower_participant["clips"][0]["duration_frames"] = 12
    assert (
        rejection_code(lambda: decode_public_snapshot(resign(no_lower_participant)))
        == "invalid_contract"
    )


def test_resolver_is_pure_ordered_integer_timed_and_audio_single_owner() -> None:
    document = fixture_document()
    snapshot = decode_public_snapshot(document["snapshot"])
    expected = document["expectations"]
    scene = resolve_composition(snapshot, expected["frame"])
    assert scene.public_fingerprint == snapshot.public_fingerprint
    assert [layer.clip_id for layer in scene.layers] == expected["layer_clip_ids"]
    assert scene.layers[0].source_frame == 12
    assert scene.layers[0].source_pts == expected["source_pts"]
    assert scene.layers[1].transition_elapsed_frames == 0
    assert scene.audio_span is not None
    assert scene.audio_span.clip_id == expected["audio_clip_id"]
    assert scene.audio_span.output_start_sample == expected["output_start_sample"]
    assert scene.audio_span.output_end_sample == expected["output_end_sample"]
    assert scene.audio_span.source_start_sample == expected["source_start_sample"]
    assert scene.audio_span.source_end_sample == expected["source_end_sample"]
    assert [list(layer.operation_ids) for layer in scene.layers] == [
        ["SelectSourceRangeV1", "CropV1", "Transform2DV1", "OpacityV1", "BlendV1"],
        [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
            "ColorAdjustV1",
            "OpacityV1",
            "BlendV1",
            "CrossDissolveV1",
        ],
        ["SelectSourceRangeV1", "CropV1", "Transform2DV1", "OpacityV1", "BlendV1"],
        [
            "SelectSourceRangeV1",
            "CropV1",
            "Transform2DV1",
            "OpacityV1",
            "BlendV1",
            "DrawTextV1",
        ],
    ]
    assert scene == resolve_composition(snapshot, expected["frame"])
    assert len(scene.to_canonical_bytes()) <= 131_072


def test_primary_overlap_has_one_audio_owner_and_video_capacity_is_fail_closed() -> None:
    wire = fixture_snapshot()
    incoming = json.loads(json.dumps(wire["clips"][0]))
    incoming.update(
        {
            "clip_id": "clip-incoming",
            "start_frame": 24,
            "duration_frames": 12,
            "source_start_frame": 0,
        }
    )
    wire["clips"].append(incoming)
    snapshot = decode_public_snapshot(resign(wire))
    scene = resolve_composition(snapshot, 24)
    assert scene.audio_span is not None
    assert scene.audio_span.clip_id == "clip-incoming"
    assert scene.audio_span.source_start_sample == 0

    over_capacity = fixture_snapshot()
    incoming["start_frame"] = 12
    over_capacity["clips"].append(incoming)
    assert rejection_code(lambda: decode_public_snapshot(resign(over_capacity))) == "resource_limit"


def test_vfr_source_start_anchors_to_an_exact_declared_landmark() -> None:
    wire = fixture_snapshot()
    asset = wire["assets"][0]
    asset["source_time_base"] = {"num": 1, "den": 1000}
    asset["landmarks"] = [
        {"frame_index": 0, "pts": 0, "dts": 0, "duration_ticks": 42},
        {"frame_index": 1, "pts": 42, "dts": 42, "duration_ticks": 41},
        {"frame_index": 2, "pts": 83, "dts": 83, "duration_ticks": 42},
    ]
    wire["clips"][0]["source_start_frame"] = 1
    snapshot = decode_public_snapshot(resign(wire))
    scene = resolve_composition(snapshot, 0)
    layer = scene.layers[0]
    assert (layer.source_frame, layer.source_pts) == (1, 42)
    assert scene.audio_span is not None
    assert scene.audio_span.source_start_sample == 2016


def test_resolver_rejects_a_target_beyond_declared_source_end() -> None:
    wire = fixture_snapshot()
    wire["assets"][0]["landmarks"][-1] = {
        "frame_index": 47,
        "pts": 12_000,
        "dts": 12_000,
        "duration_ticks": 512,
    }
    snapshot = decode_public_snapshot(resign(wire))
    assert rejection_code(lambda: resolve_composition(snapshot, 47)) == "source_range_unavailable"


def test_coarse_source_timebase_returns_audio_blocker_not_zero_length_span() -> None:
    wire = fixture_snapshot()
    wire["assets"][0]["source_time_base"] = {"num": 1, "den": 1}
    snapshot = decode_public_snapshot(resign(wire))
    scene = resolve_composition(snapshot, 0)
    assert scene.audio_span is None
    assert ("embedded_audio_unavailable", "clip-main") in {
        (blocker.code, blocker.subject_id) for blocker in scene.blockers
    }


def test_first_last_gap_and_unavailable_timing_have_no_clamp_or_fabrication() -> None:
    snapshot = decode_public_snapshot(fixture_snapshot())
    assert resolve_composition(snapshot, 0).frame == 0
    assert resolve_composition(snapshot, 47).frame == 47
    assert rejection_code(lambda: resolve_composition(snapshot, -1)) == "source_range_unavailable"
    assert rejection_code(lambda: resolve_composition(snapshot, 48)) == "source_range_unavailable"

    asset = snapshot.assets[0]
    missing = replace(asset, landmarks=())
    no_timing = replace(snapshot, assets=(missing, *snapshot.assets[1:]))
    scene = resolve_composition(no_timing, 1)
    assert scene.layers[0].source_frame is None
    assert [blocker.code for blocker in scene.blockers] == [
        "timing_unavailable",
        "embedded_audio_unavailable",
    ]


def test_operation_profile_refuses_unknown_and_independent_audio_before_mutation() -> None:
    assert {operation_disposition(operation) for operation in NLE_OPERATION_IDS} == {"accepted"}
    assert (
        rejection_code(lambda: operation_disposition("shader.plugin")) == "operation_not_in_profile"
    )
    assert (
        rejection_code(lambda: operation_disposition("h3.authoring.audio.command.v1.gain"))
        == "audio_editing_deferred"
    )


def test_private_manifests_have_no_public_wire_and_are_rejected_by_public_decoder() -> None:
    private = PrivateSourceManifest(
        schema=PRIVATE_SOURCE_MANIFEST_SCHEMA,
        asset_id="vid-primary",
        generation=1,
        source_fingerprint="sha256:" + "3" * 64,
        runtime_identity="runtime-one",
    )
    derivative = DerivativeManifest(
        schema="h3.context.derivative_manifest.v1",
        asset_id="vid-primary",
        original_fingerprint="sha256:" + "3" * 64,
        generator_profile_id="generator-one",
        derivative_fingerprint="sha256:" + "4" * 64,
        generation=1,
    )
    assert not hasattr(private, "to_wire")
    assert not hasattr(derivative, "to_wire")
    assert rejection_code(lambda: decode_public_snapshot(asdict(private))) == "private_field"


def test_additive_legacy_adapter_requires_explicit_cas_and_refuses_audio_semantics() -> None:
    projection: dict[str, Any] = {
        "schema": "h3.context.authoring_workbench.projection.v1",
        "workspace_handle": "workspace-fixture",
        "context_source_id": "source-fixture",
        "task_mode": "t2va",
        "registry_fingerprint": "sha256:" + "7" * 64,
        "reference": {},
        "availability": {},
        "timeline": {
            "revision": 2,
            "content_fingerprint": "sha256:" + "5" * 64,
            "profile": {},
            "clips": [
                {
                    "clip_id": "legacy-video",
                    "asset_id": "vid-primary",
                    "kind": "video",
                    "lane": 0,
                    "start_frame": 0,
                    "frames": 24,
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
    timing = {"vid-primary": fixture_snapshot()["assets"][0]}
    snapshot = adapt_authoring_projection(
        projection,
        project_id="project-fixture",
        workspace_revision=3,
        workspace_fingerprint="sha256:" + "6" * 64,
        output=fixture_snapshot()["output"],
        asset_timings=timing,
    )
    assert snapshot.tracks[0].track_id == "legacy.primary_video"
    assert snapshot.clips[0].clip_id == "legacy-video"

    repeated = json.loads(json.dumps(projection))
    repeated["timeline"]["clips"].append(
        {
            **repeated["timeline"]["clips"][0],
            "clip_id": "legacy-video-2",
            "start_frame": 24,
        }
    )
    repeated_snapshot = adapt_authoring_projection(
        repeated,
        project_id="project-fixture",
        workspace_revision=3,
        workspace_fingerprint="sha256:" + "6" * 64,
        output=fixture_snapshot()["output"],
        asset_timings=timing,
    )
    assert len(repeated_snapshot.assets) == 1
    assert [clip.clip_id for clip in repeated_snapshot.clips] == [
        "legacy-video",
        "legacy-video-2",
    ]

    incomplete = dict(projection)
    del incomplete["reference"]
    assert (
        rejection_code(
            lambda: adapt_authoring_projection(
                incomplete,
                project_id="project-fixture",
                workspace_revision=3,
                workspace_fingerprint="sha256:" + "6" * 64,
                output=fixture_snapshot()["output"],
                asset_timings=timing,
            )
        )
        == "invalid_contract"
    )

    private = json.loads(json.dumps(projection))
    private["reference"]["source_url"] = "private"
    assert (
        rejection_code(
            lambda: adapt_authoring_projection(
                private,
                project_id="project-fixture",
                workspace_revision=3,
                workspace_fingerprint="sha256:" + "6" * 64,
                output=fixture_snapshot()["output"],
                asset_timings=timing,
            )
        )
        == "private_field"
    )

    projection["timeline"]["links"] = [{"video_clip_id": "legacy-video", "audio_clip_id": "audio"}]
    assert (
        rejection_code(
            lambda: adapt_authoring_projection(
                projection,
                project_id="project-fixture",
                workspace_revision=3,
                workspace_fingerprint="sha256:" + "6" * 64,
                output=fixture_snapshot()["output"],
                asset_timings=timing,
            )
        )
        == "audio_editing_deferred"
    )


def test_non_nfc_text_is_rejected_instead_of_cross_language_normalized() -> None:
    wire = fixture_snapshot()
    wire["clips"][3]["text"]["content"] = "Cafe\u0301"
    assert rejection_code(lambda: decode_public_snapshot(resign(wire))) == "invalid_contract"
