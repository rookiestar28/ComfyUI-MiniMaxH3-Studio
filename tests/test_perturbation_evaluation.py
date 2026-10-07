from __future__ import annotations

import copy
import dataclasses
import json
import math
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from comfyui_h3_context.core.perturbation_evaluation import (
    MAX_MATERIAL_BYTES,
    MAX_MATERIAL_DEPTH,
    CorpusPartition,
    ExpectedRelation,
    ExpectedRelationKind,
    MissingRelationState,
    PerturbationDimension,
    PerturbationError,
    PerturbationOperation,
    PerturbationSource,
    PerturbationSpec,
    apply_perturbation,
    build_perturbation_corpus,
    decode_perturbation_corpus_json,
    validate_perturbation_corpus_wire,
)
from scripts.m14_02_perturbation_fixture import build_fixture, render_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m14_02_perturbation_evaluation.json"
SCHEMA_PATH = ROOT / "governance" / "contracts" / "perturbation_evaluation_v1.schema.json"

EXPECTED_DIMENSIONS = {
    "wording",
    "unicode",
    "metadata",
    "asset_order",
    "asset_role",
    "modality_duplicate",
    "modality_removal",
    "modality_replacement",
    "temporal_shift",
    "temporal_reverse",
    "temporal_splice",
    "subject_fusion",
    "directive_copy",
    "directive_retain",
    "directive_adapt",
    "directive_exclude",
    "ambiguity",
    "conflict",
    "duration",
    "dialogue",
    "visible_text",
    "style",
    "motion",
    "camera",
    "video_audio",
    "distractor",
    "unsupported_input",
}


def _wording_source(material: dict[str, object] | None = None) -> PerturbationSource:
    return PerturbationSource(
        source_id="source.wording",
        source_cluster_id="cluster.wording",
        partition=CorpusPartition.DEVELOPMENT,
        material={"wording": "A quiet opening."} if material is None else material,
    )


def _wording_spec(value: object = "A calm opening.") -> PerturbationSpec:
    return PerturbationSpec(
        case_id="case.wording",
        parent_id="source.wording",
        source_cluster_id="cluster.wording",
        seed=1_402_001,
        dimension=PerturbationDimension.WORDING,
        operation=PerturbationOperation.REPLACE,
        target_path="/wording",
        value=value,
        indices=(),
        delta=None,
        delete_count=None,
        expected_local_relation=ExpectedRelation(
            kind=ExpectedRelationKind.INVARIANT,
            direction=None,
            rationale_code="wording.structure.invariant",
        ),
    )


def test_frozen_corpus_covers_exact_27_dimensions_without_cartesian_expansion() -> None:
    corpus = build_fixture()

    assert {item.value for item in PerturbationDimension} == EXPECTED_DIMENSIONS
    assert len(corpus.sources) == 9
    assert len({source.source_cluster_id for source in corpus.sources}) == 9
    assert len(corpus.specs) == 27
    assert len(corpus.cases) == 27
    assert {spec.dimension.value for spec in corpus.specs} == EXPECTED_DIMENSIONS
    assert len({spec.dimension for spec in corpus.specs}) == 27
    assert corpus.source_cluster_count == 9
    assert corpus.case_count == 27
    assert tuple(spec.case_id for spec in corpus.specs) == tuple(
        sorted(spec.case_id for spec in corpus.specs)
    )
    assert set(item.operation for item in corpus.specs) == set(PerturbationOperation)


def test_each_transform_changes_exactly_its_declared_path_and_preserves_parent() -> None:
    corpus = build_fixture()
    source_map = {source.source_id: source for source in corpus.sources}

    for spec, case in zip(corpus.specs, corpus.cases, strict=True):
        source = source_map[spec.parent_id]
        before = copy.deepcopy(source.to_wire()["material"])
        rebuilt = apply_perturbation(source, spec)

        assert rebuilt == case
        assert case.parent_id == source.source_id
        assert case.source_cluster_id == source.source_cluster_id
        assert case.seed == spec.seed
        assert case.changed_paths == (spec.target_path,)
        assert case.source_fingerprint == source.fingerprint
        assert case.source_fingerprint != case.variant_fingerprint
        assert source.to_wire()["material"] == before


def test_transform_generation_is_seed_and_order_deterministic() -> None:
    first = build_fixture()
    second = build_fixture()

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.to_wire() == second.to_wire()
    assert render_fixture(first) == render_fixture(second)
    assert tuple(source.source_id for source in first.sources) == tuple(
        sorted(source.source_id for source in first.sources)
    )


def test_dimension_operation_path_join_rejects_semantic_mislabeling() -> None:
    with pytest.raises(PerturbationError, match="dimension.*operation.*path"):
        PerturbationSpec(
            case_id="case.mislabeled",
            parent_id="source.wording",
            source_cluster_id="cluster.wording",
            seed=1,
            dimension=PerturbationDimension.DURATION,
            operation=PerturbationOperation.REPLACE,
            target_path="/wording",
            value="forged",
            indices=(),
            delta=None,
            delete_count=None,
            expected_local_relation=ExpectedRelation(
                ExpectedRelationKind.MONOTONIC,
                "increase",
                "duration.increase",
            ),
        )


def test_zero_change_and_invalid_operation_parameters_fail_closed() -> None:
    source = _wording_source()

    with pytest.raises(PerturbationError, match="exactly one declared path"):
        apply_perturbation(source, _wording_spec("A quiet opening."))
    with pytest.raises(PerturbationError, match="replace parameters"):
        dataclasses.replace(_wording_spec(), delta=1)


def test_official_relation_and_agreement_are_always_explicitly_missing() -> None:
    corpus = build_fixture()

    assert corpus.official_relation_state is MissingRelationState.MISSING
    assert corpus.local_official_agreement_state is MissingRelationState.MISSING
    for case in corpus.cases:
        assert case.official_relation.state is MissingRelationState.MISSING
        assert case.official_relation.value is None
        assert case.local_official_agreement.state is MissingRelationState.MISSING
        assert case.local_official_agreement.value is None
        assert case.expected_local_relation.kind in ExpectedRelationKind


def test_offline_program_reaches_no_provider_network_media_or_process_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("external execution boundary was reached")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)

    corpus = build_fixture()
    validated = validate_perturbation_corpus_wire(corpus.to_wire())

    assert validated == corpus
    assert validated.official_relation_state is MissingRelationState.MISSING
    assert validated.local_official_agreement_state is MissingRelationState.MISSING


@pytest.mark.parametrize(
    "mutation",
    (
        "root_open",
        "source_material",
        "spec_path",
        "case_variant",
        "case_changed_paths",
        "case_fingerprint",
        "case_official_relation",
        "case_agreement",
        "aggregate_count",
        "aggregate_dimensions",
        "corpus_fingerprint",
    ),
)
def test_wire_validator_regenerates_and_rejects_variant_or_summary_drift(
    mutation: str,
) -> None:
    wire: dict[str, Any] = copy.deepcopy(build_fixture().to_wire())
    if mutation == "root_open":
        wire["unexpected"] = True
    elif mutation == "source_material":
        wire["sources"][0]["material"]["wording"] = "forged"
    elif mutation == "spec_path":
        wire["specs"][0]["target_path"] = "/metadata"
    elif mutation == "case_variant":
        wire["cases"][0]["variant_material"]["ambiguity"] = "forged"
    elif mutation == "case_changed_paths":
        wire["cases"][0]["changed_paths"].append("/forged")
    elif mutation == "case_fingerprint":
        wire["cases"][0]["variant_fingerprint"] = "sha256:" + "f" * 64
    elif mutation == "case_official_relation":
        wire["cases"][0]["official_relation"] = {"state": "recorded", "value": "matched"}
    elif mutation == "case_agreement":
        wire["cases"][0]["local_official_agreement"] = {
            "state": "recorded",
            "value": "1",
        }
    elif mutation == "aggregate_count":
        wire["case_count"] = 26
    elif mutation == "aggregate_dimensions":
        wire["dimensions"] = wire["dimensions"][:-1]
    elif mutation == "corpus_fingerprint":
        wire["corpus_fingerprint"] = "sha256:" + "0" * 64
    else:  # pragma: no cover - exhaustive parameter inventory above
        raise AssertionError(mutation)

    with pytest.raises(PerturbationError):
        validate_perturbation_corpus_wire(wire)


def test_wire_validator_rejects_self_consistent_frozen_program_drift() -> None:
    genuine = build_fixture()
    first = genuine.sources[0]
    material = cast(dict[str, object], copy.deepcopy(first.to_wire()["material"]))
    material["wording"] = "A self-consistent but unregistered source revision."
    drifted_source = PerturbationSource(
        first.source_id,
        first.source_cluster_id,
        first.partition,
        material,
    )
    drifted = build_perturbation_corpus(
        genuine.corpus_id,
        genuine.corpus_version,
        (drifted_source, *genuine.sources[1:]),
        genuine.specs,
    )
    assert drifted.fingerprint != genuine.fingerprint

    with pytest.raises(PerturbationError, match="frozen v1 program"):
        validate_perturbation_corpus_wire(drifted.to_wire())


def test_public_case_and_corpus_constructors_reject_derived_forgery() -> None:
    genuine = build_fixture()
    case = genuine.cases[0]

    with pytest.raises(PerturbationError):
        dataclasses.replace(case, variant_fingerprint="sha256:" + "f" * 64)
    with pytest.raises(PerturbationError):
        dataclasses.replace(case, source_fingerprint="sha256:" + "f" * 64)
    with pytest.raises(PerturbationError):
        dataclasses.replace(case, dimension=PerturbationDimension.UNICODE)
    with pytest.raises(PerturbationError):
        dataclasses.replace(genuine, cases=tuple(reversed(genuine.cases)))
    with pytest.raises(PerturbationError):
        dataclasses.replace(
            genuine,
            specs=(genuine.specs[0],) * 27,
            cases=(genuine.cases[0],) * 27,
        )
    with pytest.raises(PerturbationError):
        dataclasses.replace(genuine, sources=tuple(reversed(genuine.sources)))


def test_duplicate_open_oversized_deep_nonfinite_and_sensitive_material_fail_closed() -> None:
    genuine = render_fixture(build_fixture()).decode("utf-8")
    duplicate = genuine.replace(
        '  "schema": "h3.context.perturbation.evaluation.v1",',
        '  "schema": "h3.context.perturbation.evaluation.v1",\n'
        '  "schema": "h3.context.perturbation.evaluation.v1",',
        1,
    )
    with pytest.raises(PerturbationError, match="duplicate"):
        decode_perturbation_corpus_json(duplicate)

    with pytest.raises(PerturbationError, match="sensitive"):
        _wording_source({"credential": "not-a-real-secret"})
    with pytest.raises(PerturbationError, match="unsafe locator"):
        _wording_source({"wording": "https://example.invalid/private"})
    with pytest.raises(PerturbationError, match="unsafe locator"):
        _wording_source({"wording": "/private/local/path"})
    with pytest.raises(PerturbationError, match="unsafe locator"):
        _wording_source({"wording": "C:\\private\\local"})
    with pytest.raises(PerturbationError, match="finite"):
        _wording_source({"score": math.inf})

    deep: object = "leaf"
    for index in range(MAX_MATERIAL_DEPTH + 1):
        deep = {f"level_{index}": deep}
    with pytest.raises(PerturbationError, match="depth"):
        _wording_source({"wording": deep})


def test_schema_and_python_contract_accept_genuine_fixture_and_reject_mutations() -> None:
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    jsonschema.validate(wire, schema)
    validated = validate_perturbation_corpus_wire(wire)
    assert validated.to_wire() == wire

    open_case = copy.deepcopy(wire)
    open_case["cases"][0]["unexpected"] = "forged"
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(open_case))
    with pytest.raises(PerturbationError):
        validate_perturbation_corpus_wire(open_case)


def test_portable_schema_rejects_python_security_and_resource_violations() -> None:
    genuine: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    deep: object = "leaf"
    for index in range(MAX_MATERIAL_DEPTH + 2):
        deep = {f"level_{index}": deep}
    aggregate_leaf = [[["x" * 40 for _ in range(4)] for _ in range(4)] for _ in range(4)]
    aggregate: dict[str, object] = {f"field_{index}": aggregate_leaf for index in range(24)}
    assert (
        len(
            json.dumps(aggregate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        > MAX_MATERIAL_BYTES
    )
    unicode_leaf = [["😀" * 40 for _ in range(4)] for _ in range(4)]
    unicode_aggregate: dict[str, object] = {f"field_{index}": unicode_leaf for index in range(24)}
    integer_leaf = [[10**999 for _ in range(4)] for _ in range(4)]
    integer_aggregate: dict[str, object] = {f"field_{index}": integer_leaf for index in range(24)}
    for oversized in (unicode_aggregate, integer_aggregate):
        assert (
            len(
                json.dumps(
                    oversized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            > MAX_MATERIAL_BYTES
        )
    escaped_leaf = "a" + "\\" * 39
    escaped_shape = [{f"n{index:031d}": escaped_leaf for index in range(3)} for _ in range(4)]
    escaped_aggregate: dict[str, object] = {f"f{index:031d}": escaped_shape for index in range(24)}
    assert (
        len(
            json.dumps(
                escaped_aggregate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        > MAX_MATERIAL_BYTES
    )

    unsafe_materials: tuple[dict[str, object], ...] = (
        {"credential": "not-a-real-secret"},
        {"wording": "https://example.invalid/private"},
        {"wording": "prefix C:\\private\\local"},
        {"wording": "safe\nline"},
        {"score": math.inf},
        {"wording": deep},
        {f"field_{index}": "x" * 1_024 for index in range(64)},
        aggregate,
        unicode_aggregate,
        integer_aggregate,
        escaped_aggregate,
        {"wording": "\ud800"},
        {"wording": "\udfff"},
    )
    for material in unsafe_materials:
        mutated = copy.deepcopy(genuine)
        mutated["sources"][0]["material"] = material
        assert list(validator.iter_errors(mutated)), material
        with pytest.raises(PerturbationError):
            validate_perturbation_corpus_wire(mutated)

    oversized_parameter = copy.deepcopy(genuine)
    oversized_parameter["specs"][0]["parameters"]["value"] = [
        [10**3_999 for _ in range(4)] for _ in range(4)
    ]
    assert list(validator.iter_errors(oversized_parameter))
    with pytest.raises(PerturbationError):
        validate_perturbation_corpus_wire(oversized_parameter)


def test_portable_schema_admitted_worst_case_fanouts_fit_material_byte_budget() -> None:
    genuine: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    scalar = "😀" * 10
    assert len(scalar.encode("utf-8")) == 40

    maximum_shapes: tuple[object, ...] = (
        [[scalar for _ in range(4)] for _ in range(4)],
        [{f"n{index:031d}": scalar for index in range(3)} for _ in range(4)],
        {f"n{index:031d}": [scalar for _ in range(4)] for index in range(3)},
        {f"n{index:031d}": {f"i{inner:031d}": scalar for inner in range(3)} for index in range(3)},
    )
    for shape in maximum_shapes:
        material = {f"f{index:031d}": shape for index in range(24)}
        encoded = json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert len(encoded) <= MAX_MATERIAL_BYTES
        mutated = copy.deepcopy(genuine)
        mutated["sources"][0]["material"] = material
        assert not list(validator.iter_errors(mutated))


def test_fixture_generator_check_is_byte_identical() -> None:
    expected = render_fixture(build_fixture())
    assert expected == FIXTURE_PATH.read_bytes()
