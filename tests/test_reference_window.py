"""Independent native Ref2VA truncation oracle and closed window contract."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from comfyui_h3_context.core import (
    ReferenceConditioningWindow,
    floor_frame_count,
    reference_conditioning_window,
)
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.length import LengthError


def native_window(
    reference_count: int, output_count: int
) -> tuple[int, tuple[int, ...], tuple[Decimal, ...]]:
    # Independent transcription of ComfyUI Ref2VA: truncate, reject, then floor 17k+5.
    count = min(reference_count, output_count)
    if count < 5:
        raise ValueError("reference_too_short")
    while count % 17 != 5:
        count -= 1
    indices = tuple(range(0, count, 12))
    return count, indices, tuple(Decimal(index) / 2 for index in range(len(indices)))


def assert_native_pair(reference_count: int, output_count: int) -> None:
    if reference_count < 5:
        with pytest.raises(ContractValidationError, match="reference_too_short"):
            reference_conditioning_window(output_count, reference_count)
        return
    expected_count, indices, timestamps = native_window(reference_count, output_count)
    actual = reference_conditioning_window(output_count, reference_count)
    assert actual.window_frame_count == expected_count
    assert actual.sample_indices == indices
    assert actual.native_timestamps == timestamps


@pytest.mark.parametrize("reference_count", range(1, 301))
def test_every_admitted_reference_against_every_output_lattice(reference_count: int) -> None:
    for output_count in range(5, 363, 17):
        assert_native_pair(reference_count, output_count)


@settings(max_examples=400, deadline=None)
@given(reference_count=st.integers(1, 300), output_count=st.sampled_from(tuple(range(5, 363, 17))))
def test_native_window_property(reference_count: int, output_count: int) -> None:
    assert_native_pair(reference_count, output_count)


@pytest.mark.parametrize(
    "reference_count,output_count,expected",
    [(288, 124, 124), (100, 362, 90), (130, 362, 124), (300, 362, 294)],
)
def test_reference_window_examples(reference_count: int, output_count: int, expected: int) -> None:
    window = reference_conditioning_window(output_count, reference_count)
    assert window.window_frame_count == expected
    assert window.to_wire() == {
        "window_frame_count": expected,
        "sample_indices": list(range(0, expected, 12)),
        "native_timestamps": [
            format(Decimal(index) / 2, "f") for index in range(len(range(0, expected, 12)))
        ],
    }


@pytest.mark.parametrize("value", [1, 2, 3, 4])
def test_floor_rejects_too_short_references(value: int) -> None:
    with pytest.raises(LengthError) as error:
        floor_frame_count(value)
    assert error.value.code == "reference_too_short"


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.0, None, "124", 3601])
def test_floor_has_exact_bounded_integer_inputs(value: object) -> None:
    with pytest.raises(LengthError):
        floor_frame_count(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "output_count,reference_count",
    [(True, 124), (124, True), (123, 124), (0, 124), (124, 0), (124, 3601), (3601, 124)],
)
def test_window_rejects_invalid_or_non_lattice_counts(
    output_count: int, reference_count: int
) -> None:
    with pytest.raises(ContractValidationError):
        reference_conditioning_window(output_count, reference_count)


@pytest.mark.parametrize(
    "count,indices,timestamps",
    [
        (124, (0, 120), (Decimal(0), Decimal("0.5"))),
        (124, tuple(range(0, 124, 12)), tuple(Decimal(index) for index in range(11))),
        (5, (False,), (Decimal(0),)),
        (5, (0,), (0,)),
        (4, (0,), (Decimal(0),)),
    ],
)
def test_window_rejects_forged_schedule(
    count: int, indices: tuple[int, ...], timestamps: tuple[object, ...]
) -> None:
    with pytest.raises(ContractValidationError):
        ReferenceConditioningWindow(count, indices, timestamps)  # type: ignore[arg-type]
