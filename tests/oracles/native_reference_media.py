"""Independent reference-frame window and native conditioning schedule.

Authority: MiniMaxH3ReferenceToVideo.execute at the temporal oracle's public host revision.
The smaller frame range is rounded down onto the five-plus-seventeen lattice. Samples
are spaced twelve video frames apart and presented with a half-second timestamp spacing.
"""

from __future__ import annotations

from decimal import Decimal
from math import sqrt

SOURCE_ANCHORS = (
    "native.reference.window@b323a345",
    "native.reference.canvas@b323a345",
    "native.mask.blend@b323a345",
)


def native_window(
    reference_count: int, output_count: int
) -> tuple[int, tuple[int, ...], tuple[Decimal, ...]]:
    available = min(reference_count, output_count)
    if available < 5:
        raise ValueError("reference_too_short")
    groups = (available - 5) // 17
    frames = 5 + groups * 17
    selected = tuple(range(0, frames, 12))
    timestamps = tuple(Decimal(position) / Decimal(2) for position in range(len(selected)))
    return frames, selected, timestamps


def canvas_size(width: int, height: int) -> tuple[int, int]:
    """A768-pixel short edge subject to a768*1344 area cap and nearest32-pixel axes."""
    factor = min(768 / min(width, height), sqrt((768 * 1344) / (width * height)))
    axes = [max(1, round(dimension * factor / 32)) * 32 for dimension in (width, height)]
    return axes[0], axes[1]


def mask_keeps_reference(mask_value: float) -> bool:
    """Zero denotes retained reference conditioning; one permits generation."""
    return mask_value == 0
