"""Pure native Ref2VA truncation and index-based Qwen sampling schedule."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .errors import ContractValidationError
from .length import FPS, MAX_FRAME_COUNT, LengthError, floor_frame_count, is_producible


@dataclass(frozen=True, slots=True)
class ReferenceConditioningWindow:
    window_frame_count: int
    sample_indices: tuple[int, ...]
    native_timestamps: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if type(self.window_frame_count) is not int or not is_producible(self.window_frame_count):
            raise ContractValidationError("invalid reference window count")
        expected = tuple(range(0, self.window_frame_count, FPS // 2))
        if (
            type(self.sample_indices) is not tuple
            or any(type(value) is not int for value in self.sample_indices)
            or self.sample_indices != expected
            or type(self.native_timestamps) is not tuple
            or any(type(value) is not Decimal for value in self.native_timestamps)
            or self.native_timestamps != tuple(Decimal(index) / 2 for index in range(len(expected)))
        ):
            raise ContractValidationError("invalid native reference schedule")

    def to_wire(self) -> dict[str, object]:
        return {
            "window_frame_count": self.window_frame_count,
            "sample_indices": list(self.sample_indices),
            "native_timestamps": [format(value, "f") for value in self.native_timestamps],
        }


def reference_conditioning_window(
    effective_frame_count: int, reference_frame_count: int
) -> ReferenceConditioningWindow:
    if (
        type(effective_frame_count) is not int
        or not is_producible(effective_frame_count)
        or type(reference_frame_count) is not int
        or not 1 <= reference_frame_count <= MAX_FRAME_COUNT
    ):
        raise ContractValidationError("invalid reference conditioning counts")
    try:
        count = floor_frame_count(min(effective_frame_count, reference_frame_count))
    except LengthError as error:
        raise ContractValidationError(error.code) from error
    indices = tuple(range(0, count, FPS // 2))
    return ReferenceConditioningWindow(
        count, indices, tuple(Decimal(index) / 2 for index in range(len(indices)))
    )
