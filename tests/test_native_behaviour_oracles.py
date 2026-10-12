"""Independent model/guide/native facts and their actual product consumers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from oracles import model_card_limits
from oracles.guide_grammar import (
    FULL_REFERENCE_FIELDS,
    detailed_description_has_shot_lines,
    shot_sequence,
    shot_starts_are_ordered,
)
from oracles.host_queue_validation import validation_call
from oracles.native_reference_media import canvas_size, mask_keeps_reference
from oracles.native_temporal import native_temporal_shape
from oracles.template_facts import load_template_facts

from comfyui_h3_context.core import native_h3
from comfyui_h3_context.core.dialogue_language import OFFICIAL_STABLE_DIALOGUE_LANGUAGES


def test_model_card_reference_limits_match_the_native_binding_contract() -> None:
    assert native_h3.NATIVE_H3_BASE_MAX_IMAGES == model_card_limits.MAX_IMAGES
    assert native_h3.NATIVE_H3_BASE_MAX_VIDEOS == model_card_limits.MAX_VIDEOS
    assert native_h3.NATIVE_H3_BASE_MAX_AUDIO == model_card_limits.MAX_AUDIOS
    assert native_h3.NATIVE_H3_BASE_MAX_REFERENCES == model_card_limits.MAX_FILES
    assert native_h3.NATIVE_H3_BASE_MIN_OUTPUT_SECONDS == Decimal(
        model_card_limits.MIN_OUTPUT_SECONDS
    )
    assert native_h3.NATIVE_H3_BASE_MAX_OUTPUT_SECONDS == Decimal(
        model_card_limits.MAX_OUTPUT_SECONDS
    )
    assert native_h3.NATIVE_H3_BASE_MIN_TIMED_REFERENCE_SECONDS == Decimal(
        model_card_limits.MIN_REFERENCE_SECONDS
    )
    assert native_h3.NATIVE_H3_BASE_MAX_TIMED_REFERENCE_SECONDS == Decimal(
        model_card_limits.MAX_REFERENCE_SECONDS
    )


def test_model_card_dialogue_language_set_is_exact() -> None:
    assert (
        frozenset(OFFICIAL_STABLE_DIALOGUE_LANGUAGES) == model_card_limits.STABLE_DIALOGUE_LANGUAGES
    )
    assert len(OFFICIAL_STABLE_DIALOGUE_LANGUAGES) == 11


def test_native_binding_refuses_the_independent_aggregate_reference_limit() -> None:
    from test_native_h3_adapter import _registry_report

    from comfyui_h3_context.core import (
        AssetRole,
        MediaKind,
        MediaMetadata,
        NativeH3AdapterError,
        ReferenceAsset,
        build_native_h3_wiring,
        build_reference_registry,
    )

    assets = [
        ReferenceAsset(f"image_{index}", MediaKind.IMAGE, AssetRole.REFERENCE, index)
        for index in range(1, model_card_limits.MAX_IMAGES + 1)
    ]
    assets.append(
        ReferenceAsset(
            "video_1",
            MediaKind.VIDEO,
            AssetRole.REFERENCE,
            10,
            metadata=MediaMetadata(duration_seconds=Decimal("2")),
        )
    )
    assets.extend(
        ReferenceAsset(
            f"audio_{index}",
            MediaKind.AUDIO,
            AssetRole.AUDIO_SOURCE,
            10 + index,
            metadata=MediaMetadata(duration_seconds=Decimal("2")),
        )
        for index in range(1, model_card_limits.MAX_AUDIOS + 1)
    )
    assert len(assets) == model_card_limits.MAX_FILES + 1
    with pytest.raises(NativeH3AdapterError, match="h3_base_total_reference_limit"):
        build_native_h3_wiring(_registry_report(build_reference_registry(tuple(assets))))


@pytest.mark.parametrize(
    "duration",
    [model_card_limits.MIN_OUTPUT_SECONDS - 0.1, model_card_limits.MAX_OUTPUT_SECONDS + 0.1],
)
def test_native_binding_refuses_output_outside_the_independent_model_range(duration: float) -> None:
    from test_native_h3_adapter import _registry_report

    from comfyui_h3_context.core import (
        AssetRole,
        MediaKind,
        NativeH3AdapterError,
        ReferenceAsset,
        build_native_h3_wiring,
        build_reference_registry,
    )

    registry = build_reference_registry(
        (ReferenceAsset("image_1", MediaKind.IMAGE, AssetRole.REFERENCE, 1),)
    )
    with pytest.raises(NativeH3AdapterError, match="h3_base_output_duration_limit"):
        build_native_h3_wiring(_registry_report(registry, duration_seconds=duration))


def test_template_facts_are_present_without_an_ignored_corpus() -> None:
    facts = load_template_facts()
    assert facts["license"] == "MIT"
    assert facts["repository"] == "Comfy-Org/workflow_templates"
    assert len(facts["revision"]) == 40
    assert {row["name"] for row in facts["templates"]} == {
        "video_minimax_h3_t2v",
        "video_minimax_h3_i2v",
        "video_minimax_h3_r2v",
    }
    for row in facts["templates"]:
        assert len(row["file_sha256"]) == 64
        assert row["model_filenames"] and row["length_expression"]


def test_published_template_facts_match_the_actual_defaults_and_digest_contract() -> None:
    from comfyui_h3_context.adapters.comfyui_generation_profile import TEMPLATE_DEFAULT_ASSETS
    from comfyui_h3_context.core.generation_profile import PINNED_TEMPLATE_DIGESTS

    facts = load_template_facts()
    assert dict(PINNED_TEMPLATE_DIGESTS) == {
        row["name"]: row["file_sha256"] for row in facts["templates"]
    }
    expected = {name for row in facts["templates"] for name in row["model_filenames"]}
    assert {name for _slot, _folder, name in TEMPLATE_DEFAULT_ASSETS} == expected


def test_full_reference_sections_follow_the_independent_guide_interface() -> None:
    from test_full_rendering import full_plan

    from comfyui_h3_context.core import render_full_reference_prompt

    document = render_full_reference_prompt(full_plan())
    assert tuple(section.heading for section in document.sections) == FULL_REFERENCE_FIELDS


def test_later_rendered_shots_have_ordered_bounded_timestamps() -> None:
    from test_full_rendering import full_plan

    from comfyui_h3_context.core import render_full_reference_prompt

    document = render_full_reference_prompt(full_plan())
    section = next(
        section for section in document.sections if section.heading == "detailed_description"
    )
    assert shot_sequence(section.body) == (1, 2)
    assert shot_starts_are_ordered(section.body, duration_milliseconds=5167)


def test_mask_polarity_matches_the_independent_native_blend_specification() -> None:
    from comfyui_h3_context.core.masked_av_continuation import MASK_GENERATE, MASK_PRESERVE

    assert mask_keeps_reference(MASK_PRESERVE)
    assert not mask_keeps_reference(MASK_GENERATE)


@pytest.mark.xfail(strict=True, reason="full-reference-shot-line-layout pending renderer repair")
def test_full_reference_detailed_description_has_one_line_per_shot() -> None:
    from test_full_rendering import full_plan

    from comfyui_h3_context.core import render_full_reference_prompt

    document = render_full_reference_prompt(full_plan())
    section = next(
        section for section in document.sections if section.heading == "detailed_description"
    )
    assert detailed_description_has_shot_lines(section.body)


@pytest.mark.xfail(strict=True, reason="audio-only-reference-ready pending model-contract repair")
def test_audio_only_reference_is_not_ready_under_the_model_card_interface() -> None:
    from test_native_h3_adapter import _audio_only_reference_report

    from comfyui_h3_context.core import build_native_h3_wiring

    assert not model_card_limits.reference_counts_admissible(0, 0, 1)
    assert not build_native_h3_wiring(_audio_only_reference_report()).queue_ready


def test_guide_line_structure_distinguishes_characterization_from_conformance() -> None:
    assert detailed_description_has_shot_lines("Declared style.\n[Shot 1] First.\n[Shot 2] Second.")
    assert not detailed_description_has_shot_lines(
        "Declared style. [Shot 1] First. [Shot 2] Second."
    )
    assert shot_sequence("[Shot 1] First.\n[Shot 2] Second.") == (1, 2)


@pytest.mark.parametrize("size,expected", [((768, 768), (768, 768)), ((1344, 768), (1344, 768))])
def test_native_canvas_specification_has_bounded_grid_examples(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert canvas_size(*size) == expected
    assert not mask_keeps_reference(1.0)
    assert mask_keeps_reference(0.0)


def test_temporal_and_queue_specifications_preserve_boundary_facts() -> None:
    assert native_temporal_shape(6) == (22, 7, 37)
    schema: dict[str, Any] = {
        "required": {"request": ("H3_REQUEST", {})},
        "optional": {"value": ("IMAGE", {})},
    }
    assert validation_call(schema, {"request", "value"}, "required_links_only") == {"request": None}
