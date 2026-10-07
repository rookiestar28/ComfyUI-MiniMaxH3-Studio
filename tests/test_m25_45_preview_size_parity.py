"""M25-45: the preview-size rule is one contract on both sides of the hand-off.

The browser decides the backing store it creates; the expectation mirror decides what a row's
browser side is asked to observe. If the two drift, nothing fails loudly: the mirror simply
decides a row is not browser observable, its landmarks stop being checked on the side that sees
the preview, and a resolution regression rides through a green corpus. So the rule, its four
frozen numbers and its quantization are asserted here against the TypeScript source itself,
extracted rather than re-stated, and the two implementations are run against the same grid.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Final

import pytest

from comfyui_h3_context.core.semantic_conformance_expect import (
    PREVIEW_LEGACY_MAX_PIXELS,
    PREVIEW_LEGACY_MAX_SIDE_PX,
    PREVIEW_MAX_PIXELS,
    PREVIEW_MAX_SIDE_PX,
    preview_scale,
    preview_size,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSITOR: Final = REPOSITORY_ROOT / "frontend" / "src" / "runtime" / "visualCompositor.ts"

#: (composition width, composition height, pane CSS width, pane CSS height, device pixel ratio).
GRID: Final = (
    (320, 180, 0.0, 0.0, 1.0),
    (320, 180, 64.0, 36.0, 1.0),
    (320, 180, 1400.0, 787.5, 2.0),
    (1280, 720, 0.0, 0.0, 1.0),
    (1280, 720, 64.0, 36.0, 1.0),
    (1280, 720, 744.0, 418.5, 1.0),
    (1280, 720, 744.0, 418.5, 2.0),
    (1280, 720, 501.0, 1000.0, 1.0),
    (1920, 1080, 0.0, 0.0, 1.0),
    (1920, 1080, 1900.0, 1068.75, 2.0),
    (1600, 600, 2000.0, 750.0, 1.0),
    (608, 1080, 700.0, 1243.4, 1.0),
    (640, 360, 900.0, 506.25, 1.5),
    (960, 540, 100.0, 56.25, 3.0),
)


def _wire(width: int, height: int) -> dict[str, Any]:
    return {"output": {"width": width, "height": height}}


def _typescript_number(name: str) -> int:
    source = COMPOSITOR.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = ([0-9_]+);", source)
    assert match is not None, f"{name} is no longer an exported constant"
    return int(match.group(1).replace("_", ""))


def test_the_four_frozen_numbers_are_the_same_on_both_sides() -> None:
    assert _typescript_number("PREVIEW_LEGACY_MAX_SIDE_PX") == PREVIEW_LEGACY_MAX_SIDE_PX
    assert _typescript_number("PREVIEW_LEGACY_MAX_PIXELS") == PREVIEW_LEGACY_MAX_PIXELS
    assert _typescript_number("PREVIEW_MAX_SIDE_PX") == PREVIEW_MAX_SIDE_PX
    assert _typescript_number("PREVIEW_MAX_PIXELS") == PREVIEW_MAX_PIXELS


def test_the_legacy_surface_is_the_floor_not_a_separate_width_and_height() -> None:
    # A composition whose legacy scale is limited by its height keeps that scale, so the preview
    # is never stretched to 320 on a side the floor did not reach.
    for width, height in ((1280, 720), (1920, 1080), (608, 1080)):
        wire = _wire(width, height)
        floor = preview_scale(wire)
        assert preview_scale(wire, pane_css_width=1.0, pane_css_height=1.0) == floor
        assert preview_size(wire, pane_css_width=1.0, pane_css_height=1.0) == preview_size(wire)


def test_an_unmeasured_box_presents_exactly_the_pre_m25_45_surface() -> None:
    legacy = {
        (width, height): min(
            1.0,
            PREVIEW_LEGACY_MAX_SIDE_PX / width,
            PREVIEW_LEGACY_MAX_SIDE_PX / height,
            math.sqrt(PREVIEW_LEGACY_MAX_PIXELS / (width * height)),
        )
        for width, height, *_ in GRID
    }
    for (width, height), scale in legacy.items():
        assert preview_scale(_wire(width, height)) == pytest.approx(scale, abs=1e-12)
    for ratio in (0.0, -1.0, math.nan, math.inf):
        assert preview_scale(
            _wire(1280, 720),
            pane_css_width=900.0,
            pane_css_height=506.0,
            device_pixel_ratio=ratio,
        ) == pytest.approx(legacy[(1280, 720)], abs=1e-12)


def test_the_backing_store_never_exceeds_the_composition_or_the_cap() -> None:
    for width, height, pane_width, pane_height, ratio in GRID:
        wire = _wire(width, height)
        backing = preview_size(
            wire,
            pane_css_width=pane_width,
            pane_css_height=pane_height,
            device_pixel_ratio=ratio,
        )
        assert backing[0] <= width and backing[1] <= height
        assert max(backing) <= PREVIEW_MAX_SIDE_PX
        assert backing[0] * backing[1] <= PREVIEW_MAX_PIXELS
        assert backing[0] % 2 == 0 and backing[1] % 2 == 0
        # The aspect ratio survives the even quantization to within one quantum on a side.
        assert abs(backing[0] / backing[1] - width / height) <= 2 / min(backing)


def test_the_javascript_grid_and_the_mirror_agree_cell_by_cell() -> None:
    # The expected column is the TypeScript function's own output, recorded from
    # `frontend/tests/visualCompositor.test.ts` and its measured cells; a change on either side
    # that is not made on both turns this red.
    expected = {
        (320, 180, 0.0, 0.0, 1.0): (320, 180),
        (320, 180, 64.0, 36.0, 1.0): (320, 180),
        (320, 180, 1400.0, 787.5, 2.0): (320, 180),
        (1280, 720, 0.0, 0.0, 1.0): (320, 180),
        (1280, 720, 64.0, 36.0, 1.0): (320, 180),
        (1280, 720, 744.0, 418.5, 1.0): (744, 418),
        (1280, 720, 744.0, 418.5, 2.0): (1280, 720),
        (1280, 720, 501.0, 1000.0, 1.0): (502, 282),
        (1920, 1080, 0.0, 0.0, 1.0): (320, 180),
        (1920, 1080, 1900.0, 1068.75, 2.0): (1920, 1080),
        (1600, 600, 2000.0, 750.0, 1.0): (1600, 600),
        (608, 1080, 700.0, 1243.4, 1.0): (608, 1080),
        (640, 360, 900.0, 506.25, 1.5): (640, 360),
        # The pane is far too small, so the legacy floor decides: exactly today's surface.
        (960, 540, 100.0, 56.25, 3.0): (320, 180),
    }
    assert set(expected) == set(GRID)
    for cell, backing in expected.items():
        width, height, pane_width, pane_height, ratio = cell
        assert (
            preview_size(
                _wire(width, height),
                pane_css_width=pane_width,
                pane_css_height=pane_height,
                device_pixel_ratio=ratio,
            )
            == backing
        ), cell
