"""Build and verify the frozen M14-02 deterministic perturbation corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from comfyui_h3_context.core.perturbation_evaluation import (
    PERTURBATION_CORPUS_VERSION,
    CorpusPartition,
    ExpectedRelation,
    ExpectedRelationKind,
    PerturbationCorpus,
    PerturbationDimension,
    PerturbationOperation,
    PerturbationSource,
    PerturbationSpec,
    build_perturbation_corpus,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "m14_02_perturbation_evaluation.json"


def _material(label: str) -> dict[str, object]:
    return {
        "ambiguity": "none",
        "assets": [
            {"asset_id": "image_a", "modality": "image", "role": "first_frame"},
            {"asset_id": "video_a", "modality": "video", "role": "reference"},
            {"asset_id": "audio_a", "modality": "audio", "role": "reference_audio"},
        ],
        "camera": "static",
        "conflicts": [],
        "dialogue": "Welcome home.",
        "directives": [{"action": "retain", "target": "subject_a"}],
        "distractors": [],
        "duration_ms": 4_000,
        "metadata": {"language": "en", "normalization": "NFC"},
        "motion": "subject_walks_slowly",
        "style": "natural_documentary",
        "subjects": [{"subject_id": "subject_a", "source": "image_a"}],
        "timeline": [
            {"end_ms": 1_000, "event_id": "event_a", "start_ms": 0},
            {"end_ms": 2_000, "event_id": "event_b", "start_ms": 1_000},
            {"end_ms": 3_000, "event_id": "event_c", "start_ms": 2_000},
        ],
        "timeline_offset_ms": 0,
        "unsupported_inputs": [],
        "video_audio": {"mode": "retain", "source": "video_a"},
        "visible_text": "OPEN",
        "wording": f"A quiet opening for {label}.",
    }


def _source(index: int, name: str, partition: CorpusPartition) -> PerturbationSource:
    return PerturbationSource(
        source_id=f"source.{index:02d}.{name}",
        source_cluster_id=f"cluster.{index:02d}.{name}",
        partition=partition,
        material=_material(name),
    )


def _relation(
    dimension: PerturbationDimension,
    kind: ExpectedRelationKind,
    direction: str | None = None,
) -> ExpectedRelation:
    return ExpectedRelation(kind, direction, f"{dimension.value}.local.expectation")


def _spec(
    index: int,
    source: PerturbationSource,
    dimension: PerturbationDimension,
    operation: PerturbationOperation,
    target_path: str,
    relation: ExpectedRelation,
    *,
    value: object | None = None,
    indices: tuple[int, ...] = (),
    delta: int | None = None,
    delete_count: int | None = None,
) -> PerturbationSpec:
    return PerturbationSpec(
        case_id=f"case.{index:02d}.{dimension.value}",
        parent_id=source.source_id,
        source_cluster_id=source.source_cluster_id,
        seed=1_402_000 + index,
        dimension=dimension,
        operation=operation,
        target_path=target_path,
        value=value,
        indices=indices,
        delta=delta,
        delete_count=delete_count,
        expected_local_relation=relation,
    )


def build_fixture() -> PerturbationCorpus:
    """Return the frozen 9-source-cluster/27-case local-only corpus."""

    sources = (
        _source(1, "language", CorpusPartition.DEVELOPMENT),
        _source(2, "assets", CorpusPartition.DEVELOPMENT),
        _source(3, "temporal", CorpusPartition.DEVELOPMENT),
        _source(4, "directives", CorpusPartition.DEVELOPMENT),
        _source(5, "uncertainty", CorpusPartition.DEVELOPMENT),
        _source(6, "exact_text", CorpusPartition.DEVELOPMENT),
        _source(7, "presentation", CorpusPartition.FROZEN_TEST),
        _source(8, "audio_distractor", CorpusPartition.FROZEN_TEST),
        _source(9, "unsupported", CorpusPartition.FROZEN_TEST),
    )
    source = {item.source_cluster_id: item for item in sources}
    language = source["cluster.01.language"]
    assets = source["cluster.02.assets"]
    temporal = source["cluster.03.temporal"]
    directives = source["cluster.04.directives"]
    uncertainty = source["cluster.05.uncertainty"]
    exact_text = source["cluster.06.exact_text"]
    presentation = source["cluster.07.presentation"]
    audio_distractor = source["cluster.08.audio_distractor"]
    unsupported = source["cluster.09.unsupported"]

    invariant = ExpectedRelationKind.INVARIANT
    covariant = ExpectedRelationKind.COVARIANT
    monotonic = ExpectedRelationKind.MONOTONIC
    unknown = ExpectedRelationKind.UNKNOWN
    specs = (
        _spec(
            1,
            language,
            PerturbationDimension.WORDING,
            PerturbationOperation.REPLACE,
            "/wording",
            _relation(PerturbationDimension.WORDING, invariant),
            value="A measured opening.",
        ),
        _spec(
            2,
            language,
            PerturbationDimension.UNICODE,
            PerturbationOperation.REPLACE,
            "/visible_text",
            _relation(PerturbationDimension.UNICODE, invariant),
            value="入口 Café",
        ),
        _spec(
            3,
            language,
            PerturbationDimension.METADATA,
            PerturbationOperation.REPLACE,
            "/metadata",
            _relation(PerturbationDimension.METADATA, invariant),
            value={"language": "zh_Hant", "normalization": "NFC"},
        ),
        _spec(
            4,
            assets,
            PerturbationDimension.ASSET_ORDER,
            PerturbationOperation.REORDER,
            "/assets",
            _relation(PerturbationDimension.ASSET_ORDER, covariant),
            indices=(2, 0, 1),
        ),
        _spec(
            5,
            assets,
            PerturbationDimension.ASSET_ROLE,
            PerturbationOperation.REPLACE,
            "/assets",
            _relation(PerturbationDimension.ASSET_ROLE, covariant),
            value=[
                {"asset_id": "image_a", "modality": "image", "role": "subject"},
                {"asset_id": "video_a", "modality": "video", "role": "style"},
                {"asset_id": "audio_a", "modality": "audio", "role": "reference_audio"},
            ],
        ),
        _spec(
            6,
            assets,
            PerturbationDimension.MODALITY_DUPLICATE,
            PerturbationOperation.DUPLICATE,
            "/assets",
            _relation(PerturbationDimension.MODALITY_DUPLICATE, covariant),
            indices=(1,),
        ),
        _spec(
            7,
            assets,
            PerturbationDimension.MODALITY_REMOVAL,
            PerturbationOperation.SPLICE,
            "/assets",
            _relation(PerturbationDimension.MODALITY_REMOVAL, covariant),
            value=[],
            indices=(1,),
            delete_count=1,
        ),
        _spec(
            8,
            assets,
            PerturbationDimension.MODALITY_REPLACEMENT,
            PerturbationOperation.REPLACE,
            "/assets",
            _relation(PerturbationDimension.MODALITY_REPLACEMENT, covariant),
            value=[
                {"asset_id": "video_b", "modality": "video", "role": "reference"},
                {"asset_id": "audio_b", "modality": "audio", "role": "reference_audio"},
            ],
        ),
        _spec(
            9,
            temporal,
            PerturbationDimension.TEMPORAL_SHIFT,
            PerturbationOperation.NUMERIC_SHIFT,
            "/timeline_offset_ms",
            _relation(PerturbationDimension.TEMPORAL_SHIFT, monotonic, "increase"),
            delta=250,
        ),
        _spec(
            10,
            temporal,
            PerturbationDimension.TEMPORAL_REVERSE,
            PerturbationOperation.REVERSE,
            "/timeline",
            _relation(PerturbationDimension.TEMPORAL_REVERSE, covariant),
        ),
        _spec(
            11,
            temporal,
            PerturbationDimension.TEMPORAL_SPLICE,
            PerturbationOperation.SPLICE,
            "/timeline",
            _relation(PerturbationDimension.TEMPORAL_SPLICE, covariant),
            value=[{"end_ms": 2_500, "event_id": "event_inserted", "start_ms": 1_500}],
            indices=(1,),
            delete_count=1,
        ),
        _spec(
            12,
            directives,
            PerturbationDimension.SUBJECT_FUSION,
            PerturbationOperation.REPLACE,
            "/subjects",
            _relation(PerturbationDimension.SUBJECT_FUSION, covariant),
            value=[{"source": "image_a+video_a", "subject_id": "subject_fused"}],
        ),
        _spec(
            13,
            directives,
            PerturbationDimension.DIRECTIVE_COPY,
            PerturbationOperation.REPLACE,
            "/directives",
            _relation(PerturbationDimension.DIRECTIVE_COPY, covariant),
            value=[{"action": "copy", "target": "subject_a"}],
        ),
        _spec(
            14,
            directives,
            PerturbationDimension.DIRECTIVE_RETAIN,
            PerturbationOperation.REPLACE,
            "/directives",
            _relation(PerturbationDimension.DIRECTIVE_RETAIN, covariant),
            value=[{"action": "retain", "target": "style"}],
        ),
        _spec(
            15,
            directives,
            PerturbationDimension.DIRECTIVE_ADAPT,
            PerturbationOperation.REPLACE,
            "/directives",
            _relation(PerturbationDimension.DIRECTIVE_ADAPT, covariant),
            value=[{"action": "adapt", "target": "motion"}],
        ),
        _spec(
            16,
            directives,
            PerturbationDimension.DIRECTIVE_EXCLUDE,
            PerturbationOperation.REMOVE,
            "/directives",
            _relation(PerturbationDimension.DIRECTIVE_EXCLUDE, covariant),
        ),
        _spec(
            17,
            uncertainty,
            PerturbationDimension.AMBIGUITY,
            PerturbationOperation.REPLACE,
            "/ambiguity",
            _relation(PerturbationDimension.AMBIGUITY, unknown),
            value="subject_reference_unclear",
        ),
        _spec(
            18,
            uncertainty,
            PerturbationDimension.CONFLICT,
            PerturbationOperation.REPLACE,
            "/conflicts",
            _relation(PerturbationDimension.CONFLICT, unknown),
            value=["camera_static_vs_tracking"],
        ),
        _spec(
            19,
            exact_text,
            PerturbationDimension.DURATION,
            PerturbationOperation.NUMERIC_SHIFT,
            "/duration_ms",
            _relation(PerturbationDimension.DURATION, monotonic, "increase"),
            delta=1_000,
        ),
        _spec(
            20,
            exact_text,
            PerturbationDimension.DIALOGUE,
            PerturbationOperation.REPLACE,
            "/dialogue",
            _relation(PerturbationDimension.DIALOGUE, covariant),
            value="Do not turn back.",
        ),
        _spec(
            21,
            exact_text,
            PerturbationDimension.VISIBLE_TEXT,
            PerturbationOperation.REPLACE,
            "/visible_text",
            _relation(PerturbationDimension.VISIBLE_TEXT, covariant),
            value="AUTHORIZED ENTRY",
        ),
        _spec(
            22,
            presentation,
            PerturbationDimension.STYLE,
            PerturbationOperation.REPLACE,
            "/style",
            _relation(PerturbationDimension.STYLE, covariant),
            value="ink_wash",
        ),
        _spec(
            23,
            presentation,
            PerturbationDimension.MOTION,
            PerturbationOperation.REPLACE,
            "/motion",
            _relation(PerturbationDimension.MOTION, covariant),
            value="subject_runs_quickly",
        ),
        _spec(
            24,
            presentation,
            PerturbationDimension.CAMERA,
            PerturbationOperation.REPLACE,
            "/camera",
            _relation(PerturbationDimension.CAMERA, covariant),
            value="slow_dolly_in",
        ),
        _spec(
            25,
            audio_distractor,
            PerturbationDimension.VIDEO_AUDIO,
            PerturbationOperation.REPLACE,
            "/video_audio",
            _relation(PerturbationDimension.VIDEO_AUDIO, covariant),
            value={"mode": "exclude", "source": "video_a"},
        ),
        _spec(
            26,
            audio_distractor,
            PerturbationDimension.DISTRACTOR,
            PerturbationOperation.APPEND,
            "/distractors",
            _relation(PerturbationDimension.DISTRACTOR, invariant),
            value="decorative_cloud",
        ),
        _spec(
            27,
            unsupported,
            PerturbationDimension.UNSUPPORTED_INPUT,
            PerturbationOperation.REPLACE,
            "/unsupported_inputs",
            _relation(PerturbationDimension.UNSUPPORTED_INPUT, unknown),
            value=["unknown_binary_modality"],
        ),
    )
    return build_perturbation_corpus(
        "m14-02-deterministic-local",
        PERTURBATION_CORPUS_VERSION,
        sources,
        specs,
    )


def render_fixture(corpus: PerturbationCorpus) -> bytes:
    """Render stable UTF-8 JSON bytes with a single trailing newline."""

    return (
        json.dumps(corpus.to_wire(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    expected = render_fixture(build_fixture())
    if arguments.check:
        if not FIXTURE_PATH.is_file() or FIXTURE_PATH.read_bytes() != expected:
            print("M14-02 PERTURBATION FIXTURE: FAIL")
            return 1
        print("M14-02 PERTURBATION FIXTURE: PASS")
        return 0
    FIXTURE_PATH.write_bytes(expected)
    print(f"M14-02 PERTURBATION FIXTURE: WROTE {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
