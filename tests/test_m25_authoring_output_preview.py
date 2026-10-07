"""Preview profile and independent parent/derivative comparison; no claimed native proof."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from dataclasses import replace
from typing import Any

import pytest
from test_m25_authoring_render_jobs import _plan
from test_m25_authoring_render_receipts import _facts

from comfyui_h3_context.core.authoring_output_protocol import OutputProtocolError


def previews() -> Any:
    name = "comfyui_h3_context.adapters.authoring_output_preview"
    assert importlib.util.find_spec(name) is not None, "bounded final preview is missing"
    return importlib.import_module(name)


def test_preview_size_fits_without_upscaling_and_uses_even_dimensions() -> None:
    module = previews()
    for source, expected in (
        ((1920, 1080), (640, 360)),
        ((640, 360), (640, 360)),
        ((320, 240), (320, 240)),
        ((1080, 1080), (360, 360)),
        ((2, 1080), (2, 360)),
        ((1920, 2), (640, 2)),
    ):
        assert module.preview_dimensions(*source) == expected
    for source in ((0, 20), (3, 20), (1922, 1080), (640, 1082), (True, 10)):
        with pytest.raises(OutputProtocolError):
            module.preview_dimensions(*source)


def test_preview_facts_preserve_complete_timing_audio_and_full_hash() -> None:
    module = previews()
    parent = _facts(_plan())
    body = b"synthetic-preview-body"
    width, height = module.preview_dimensions(parent.width, parent.height)
    facts = replace(
        parent,
        width=width,
        height=height,
        byte_length=len(body),
        output_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    assert module.validate_preview(body, facts, parent) is None
    for field, wrong in (
        ("frame_count", facts.frame_count - 1),
        ("width", width + 2),
        ("frame_rate_num", 23),
        ("other_streams", 1),
        ("color_space", "bt2020"),
        ("audio_streams", 2),
        ("video_timing_fingerprint", "sha256:" + "0" * 64),
    ):
        with pytest.raises(OutputProtocolError):
            changes: dict[str, Any] = {field: wrong}
            module.validate_preview(body, replace(facts, **changes), parent)
    with pytest.raises(OutputProtocolError):
        module.validate_preview(body + b"corrupted-tail", facts, parent)
    with pytest.raises(OutputProtocolError):
        module.validate_preview(body, replace(facts, byte_length=16 * 1024 * 1024 + 1), parent)
    audible_parent = replace(
        parent,
        audio_streams=1,
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=1,
        audio_effective_samples=parent.frame_count * 2000,
    )
    audible_facts = replace(
        facts,
        audio_streams=1,
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=1,
        audio_effective_samples=parent.frame_count * 2000,
    )
    module.validate_preview(body, audible_facts, audible_parent)
    with pytest.raises(OutputProtocolError):
        module.validate_preview(
            body, replace(audible_facts, audio_effective_samples=1), audible_parent
        )
