"""Sampling reuses declared facts without retaining a caller's mutable composition."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core import semantic_conformance_expect as expect
from comfyui_h3_context.core.semantic_conformance_cases import BASE_FIXTURE_NAMES, CORPUS_BASE
from scripts import nle_semantic_report as report

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def base() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURES / BASE_FIXTURE_NAMES[CORPUS_BASE]).read_text(encoding="utf-8"))[
            "snapshot"
        ],
    )


@pytest.mark.parametrize(
    "name",
    [
        "derive_source_mapping",
        "derive_alphas",
        "patch_sample_points",
        "derive_patch_samples",
        "derive_expectation",
        "required_landmarks",
    ],
)
@pytest.mark.parametrize("kwargs", [{}, {"unexpected": True}])
def test_missing_wire_retains_the_native_signature_refusal(
    name: str, kwargs: dict[str, bool]
) -> None:
    with pytest.raises(TypeError):
        getattr(expect, name)(**kwargs)


def test_one_sampling_call_parses_each_declared_landmark_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = base()
    original = expect._int
    parsed: list[int] = []

    def counting(section: Mapping[str, Any], field: str) -> int:
        if field == "frame_index":
            parsed.append(id(section))
        return original(section, field)

    monkeypatch.setattr(expect, "_int", counting)
    samples = expect.derive_patch_samples(wire)
    assert samples
    declared = sum(len(asset.get("landmarks", [])) for asset in wire["assets"])
    assert 0 < len(parsed) <= declared, "sampling reparses the same immutable source table"


def test_a_later_public_call_observes_changed_source_facts() -> None:
    wire = base()
    before = expect.derive_source_mapping(wire)
    for clip in wire["clips"]:
        if clip.get("asset_id") and clip.get("source_start_frame") == 0:
            clip["source_start_frame"] = 1
    after = expect.derive_source_mapping(wire)
    assert before != after
    assert expect.derive_source_mapping(base()) == before


def test_sampling_does_not_copy_or_evaluate_unobserved_custom_objects() -> None:
    class Unobserved:
        def __deepcopy__(self, memo: dict[int, object]) -> Any:
            raise AssertionError("an unknown custom object must not be evaluated")

    wire = base()
    expected = expect.derive_patch_samples(wire)
    wire["unused_extension"] = Unobserved()
    assert expect.derive_patch_samples(wire) == expected


def test_bad_observed_facts_still_refuse_and_do_not_poison_the_next_call() -> None:
    valid = base()
    expected = expect.derive_patch_samples(valid)
    malformed = copy.deepcopy(valid)
    malformed["output"]["width"] = "not-an-integer"
    with pytest.raises(expect.ExpectationError, match="width is not a declared integer"):
        expect.derive_patch_samples(malformed)
    assert expect.derive_patch_samples(valid) == expected


def test_fixture_decoding_is_reused_but_actual_byte_changes_and_mutation_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for filename in BASE_FIXTURE_NAMES.values():
        (tmp_path / filename).write_bytes((FIXTURES / filename).read_bytes())
    monkeypatch.setattr(report, "FIXTURES_DIR", tmp_path)
    original = json.loads
    decoded: list[bool] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        decoded.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting)
    first = report._corpus_bases()
    expected = copy.deepcopy(first)
    first[CORPUS_BASE]["output"]["width"] = -1
    assert report._corpus_bases() == expected
    assert len(decoded) == len(BASE_FIXTURE_NAMES), (
        "unchanged actual fixture bytes were decoded again"
    )
    path = tmp_path / BASE_FIXTURE_NAMES[CORPUS_BASE]
    document = original(path.read_text(encoding="utf-8"))
    document["snapshot"]["output"]["width"] += 1
    path.write_text(json.dumps(document), encoding="utf-8")
    assert (
        report._corpus_bases()[CORPUS_BASE]["output"]["width"]
        == document["snapshot"]["output"]["width"]
    )
    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        report._corpus_bases()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        report._corpus_bases()
