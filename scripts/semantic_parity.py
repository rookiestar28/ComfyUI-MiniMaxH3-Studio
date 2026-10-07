"""M23-46 exact-byte Python/TypeScript semantic differential corpus.

The corpus is generated from the accepted M25-10 fixture.  Product validators stay independent;
this test-only runner normalizes only stable fingerprints, projections, and rejection fields.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from comfyui_h3_context.core.composition_contract import (
    CompositionContractError,
    PublicCompositionSnapshot,
    ResolvedScene,
    decode_public_snapshot,
    operation_disposition,
    public_snapshot_fingerprint,
    resolve_composition,
)

CORPUS_SCHEMA: Final = "h3.context.semantic_parity_corpus.v1"
REPORT_SCHEMA: Final = "h3.context.semantic_parity_report.v1"
MAX_CORPUS_BYTES: Final = 1_000_000
MAX_CASES: Final = 128
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "tests" / "fixtures" / "m25_10_composition_contract_v1.json"
DEFAULT_CORPUS = ROOT / "tests" / "fixtures" / "m23_46_semantic_parity_v1.json"


class SemanticParityError(ValueError):
    """Stable fail-closed error for corpus generation and report comparison."""


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _closed_json(raw: bytes, name: str) -> object:
    if len(raw) > MAX_CORPUS_BYTES:
        raise SemanticParityError(f"{name} exceeds its byte budget")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SemanticParityError(f"{name} contains a duplicate member")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=closed_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SemanticParityError(f"{name} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SemanticParityError(f"{name} is not closed UTF-8 JSON") from exc


def _object(value: object, keys: Sequence[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SemanticParityError(f"{name} must be a closed object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list) or len(value) > MAX_CASES:
        raise SemanticParityError(f"{name} must be a bounded array")
    return value


def _reverse_objects(value: object) -> object:
    if isinstance(value, dict):
        return {key: _reverse_objects(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_objects(item) for item in value]
    return value


#: Field order for the flattened layer row.  CRITICAL: both engines emit positional rows rather
#: than objects because their decoded records disagree on naming -- Python keeps the snake_case wire
#: names and the TypeScript codec returns camelCase.  Comparing objects would either force one side
#: to rename the other's output inside the runner, which is where a divergence would get quietly
#: normalized away, or compare nothing at all.  A positional row cannot be reconciled by accident.
_TRANSFORM_KEYS = (
    "anchor_x_bp",
    "anchor_y_bp",
    "position_x_bp",
    "position_y_bp",
    "scale_x_bp",
    "scale_y_bp",
    "rotation_mdeg",
)
_CROP_KEYS = ("left_bp", "top_bp", "right_bp", "bottom_bp")


def _layer_row(layer: object) -> list[object]:
    transform = layer.transform.to_wire()  # type: ignore[attr-defined]
    crop = layer.crop.to_wire()  # type: ignore[attr-defined]
    text = None if layer.text is None else layer.text.to_wire()  # type: ignore[attr-defined]
    effect = layer.effect.to_wire()  # type: ignore[attr-defined]
    return [
        layer.clip_id,  # type: ignore[attr-defined]
        layer.asset_id,  # type: ignore[attr-defined]
        layer.track_id,  # type: ignore[attr-defined]
        layer.source_frame,  # type: ignore[attr-defined]
        layer.source_pts,  # type: ignore[attr-defined]
        layer.transition_elapsed_frames,  # type: ignore[attr-defined]
        list(layer.operation_ids),  # type: ignore[attr-defined]
        [transform[key] for key in _TRANSFORM_KEYS],
        [crop[key] for key in _CROP_KEYS],
        layer.opacity_bp,  # type: ignore[attr-defined]
        layer.blend,  # type: ignore[attr-defined]
        None
        if text is None
        else [
            text["content"],
            text["font_asset_id"],
            text["size_px"],
            text["weight"],
            text["style"],
            text["align"],
            text["line_height_bp"],
            text["fill_rgba"],
            text["background_rgba"],
        ],
        [
            effect["kind"],
            effect["brightness_permille"],
            effect["contrast_permille"],
            effect["saturation_permille"],
        ],
    ]


def _scene_projection(scene: ResolvedScene) -> dict[str, object]:
    return {
        "frame": scene.frame,
        "layers": [_layer_row(layer) for layer in scene.layers],
        "audio_span": None if scene.audio_span is None else scene.audio_span.to_wire(),
        "blockers": [item.to_wire() for item in scene.blockers],
    }


def _snapshot_projection(
    snapshot: PublicCompositionSnapshot, scenes: Sequence[ResolvedScene]
) -> dict[str, object]:
    return {
        "schema": snapshot.schema,
        "profile_id": snapshot.profile_id,
        "operation_profile_id": snapshot.operation_profile_id,
        "public_fingerprint": snapshot.public_fingerprint,
        "output": {
            "duration_frames": snapshot.output.duration_frames,
            "frame_rate": snapshot.output.frame_rate.to_wire(),
            "time_base": snapshot.output.time_base.to_wire(),
        },
        "assets": [
            {
                "asset_id": item.asset_id,
                "kind": item.kind,
                "source_time_base": (
                    None if item.source_time_base is None else item.source_time_base.to_wire()
                ),
                "source_frame_count": item.source_frame_count,
                "source_sample_count": item.source_sample_count,
                "embedded_audio": item.embedded_audio,
                "timestamp_policy": item.timestamp_policy,
                "landmarks": [
                    [row.frame_index, row.pts, row.dts, row.duration_ticks]
                    for row in item.landmarks
                ],
            }
            for item in snapshot.assets
        ],
        "tracks": [
            [item.track_id, item.kind, item.order, item.enabled, item.locked]
            for item in snapshot.tracks
        ],
        "clips": [
            [
                item.clip_id,
                item.asset_id,
                item.track_id,
                item.start_frame,
                item.duration_frames,
                item.source_start_frame,
                item.enabled,
            ]
            for item in snapshot.clips
        ],
        "audio_extension": {
            "command_namespace": snapshot.audio_extension.command_namespace,
            "command_members": list(snapshot.audio_extension.command_members),
            "preview_edit_capability": snapshot.audio_extension.preview_edit_capability,
            "final_render_edit_capability": snapshot.audio_extension.final_render_edit_capability,
        },
        "resolved_scenes": [_scene_projection(scene) for scene in scenes],
    }


#: CRITICAL: the row a runner emits carries only what that runner observed.  An earlier draft also
#: carried the mutated JSON pointer, copied out of the corpus by both engines -- so the two always
#: agreed on it no matter what either engine did, and comparing it proved nothing while looking like
#: coverage.  The pointer is still recorded, as `mutation_path` on the case, where it documents what
#: the case changes without pretending to be a cross-engine signal.
_ROW_KEYS = (
    "case_id",
    "status",
    "category",
    "code",
    "canonical_fingerprint",
    "projection",
)


def _accepted_row(case_id: str, fingerprint: str | None, projection: object) -> dict[str, object]:
    return {
        "case_id": case_id,
        "status": "accepted",
        "category": None,
        "code": None,
        "canonical_fingerprint": fingerprint,
        "projection": projection,
    }


def _rejected_row(case_id: str, code: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "status": "rejected",
        "category": "contract",
        "code": code,
        "canonical_fingerprint": None,
        "projection": None,
    }


def _subject(
    case_id: str, snapshot_wire: dict[str, object], frames: Sequence[int]
) -> dict[str, object]:
    snapshot = decode_public_snapshot(snapshot_wire)
    scenes = [resolve_composition(snapshot, frame) for frame in frames]
    return {
        "case_id": case_id,
        "snapshot": snapshot_wire,
        "resolve_frames": list(frames),
        "resolved_scenes": [scene.to_wire() for scene in scenes],
        "expected": _accepted_row(
            case_id,
            snapshot.public_fingerprint,
            _snapshot_projection(snapshot, scenes),
        ),
    }


def build_corpus_bytes(source: Path = DEFAULT_SOURCE) -> bytes:
    source_raw = source.read_bytes()
    fixture = _object(
        _closed_json(source_raw, "source fixture"),
        ("schema", "snapshot", "expectations"),
        "source fixture",
    )
    snapshot_wire = cast(dict[str, object], copy.deepcopy(fixture["snapshot"]))
    decode_public_snapshot(snapshot_wire)
    reordered = cast(dict[str, object], _reverse_objects(snapshot_wire))
    frames = (0, 12, 23)
    subjects = [
        _subject("accept.baseline", snapshot_wire, frames),
        _subject("accept.recursive_key_reordering", reordered, frames),
    ]
    mutations: list[dict[str, object]] = [
        {
            "case_id": "reject.fractional_frame_count",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/assets/0/source_frame_count", "value": 47.5}],
            "mutation_path": "/assets/0/source_frame_count",
            "expected": _rejected_row("reject.fractional_frame_count", "invalid_contract"),
        },
        {
            "case_id": "reject.unreduced_source_timebase",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/assets/0/source_time_base/num", "value": 2}],
            "mutation_path": "/assets/0/source_time_base",
            "expected": _rejected_row("reject.unreduced_source_timebase", "invalid_timing"),
        },
        {
            "case_id": "reject.negative_pts",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/assets/0/landmarks/1/pts", "value": -1}],
            "mutation_path": "/assets/0/landmarks/1/pts",
            "expected": _rejected_row("reject.negative_pts", "negative_timestamp"),
        },
        {
            "case_id": "reject.nonmonotonic_pts",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/assets/0/landmarks/1/pts", "value": 0}],
            "mutation_path": "/assets/0/landmarks/1/pts",
            "expected": _rejected_row("reject.nonmonotonic_pts", "invalid_timing"),
        },
        {
            "case_id": "reject.duplicate_track_order",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/tracks/1/order", "value": 0}],
            "mutation_path": "/tracks/1/order",
            "expected": _rejected_row("reject.duplicate_track_order", "invalid_contract"),
        },
        {
            "case_id": "reject.output_timebase_profile",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/output/time_base/den", "value": 12_288}],
            "mutation_path": "/output/time_base",
            "expected": _rejected_row("reject.output_timebase_profile", "invalid_contract"),
        },
        {
            "case_id": "reject.independent_audio_members",
            "subject_id": "accept.baseline",
            # CRITICAL: re-signed, and it must stay that way.  Both engines validate the public
            # fingerprint before they reach the audio extension, so an unsigned mutation is refused
            # as `stale_snapshot` and the rule this case exists for is never executed.  The first
            # draft of this corpus asserted `audio_editing_deferred` here and had never been run.
            "mutations": [
                {
                    "op": "replace",
                    "path": "/audio_extension/command_members",
                    "value": ["insert"],
                },
                {"op": "resign", "path": "", "value": None},
            ],
            "mutation_path": "/audio_extension/command_members",
            "expected": _rejected_row("reject.independent_audio_members", "audio_editing_deferred"),
        },
        {
            # CRITICAL: this reaches the generic reserved-private-field guard, not
            # `decode_public_snapshot`'s dedicated private-manifest schema check -- the replacement
            # object carries `runtime_identity`, and the reserved-field scan runs first.  It is
            # therefore the same guard as `reject.private_source_url` below, reached from a
            # different input (a whole-root replacement rather than an added member), and the pair
            # is worth keeping for that reason.  What it does not do is exercise the dedicated
            # check, which has no TypeScript counterpart by design and so was never something a
            # cross-language corpus could compare.
            "case_id": "reject.private_source_manifest_at_public_boundary",
            "subject_id": "accept.baseline",
            "mutations": [
                {
                    "op": "replace",
                    "path": "",
                    "value": {
                        "schema": "h3.context.private_source_manifest.v1",
                        "asset_id": "vid-primary",
                        "generation": 1,
                        "source_fingerprint": "sha256:" + "1" * 64,
                        "runtime_identity": "private-runtime",
                    },
                }
            ],
            "mutation_path": "/",
            "expected": _rejected_row(
                "reject.private_source_manifest_at_public_boundary", "private_field"
            ),
        },
        {
            "case_id": "reject.private_source_url",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "add", "path": "/source_url", "value": "private"}],
            "mutation_path": "/source_url",
            "expected": _rejected_row("reject.private_source_url", "private_field"),
        },
        {
            "case_id": "reject.stale_fingerprint",
            "subject_id": "accept.baseline",
            "mutations": [{"op": "replace", "path": "/project_id", "value": "project-drift"}],
            "mutation_path": "/public_fingerprint",
            "expected": _rejected_row("reject.stale_fingerprint", "stale_snapshot"),
        },
        {
            "case_id": "reject.source_range",
            "subject_id": "accept.baseline",
            # 72 is the admitted source frame count of `vid-primary`, and the rule refuses an
            # offset at or past it.  The first draft used 48 -- the *output* duration -- which is a
            # legal offset into a 72-frame source, so the snapshot was structurally fine and died at
            # the fingerprint instead.
            #
            # CRITICAL: this case must NOT re-sign, and an earlier revision of it did.  Unlike the
            # audio-extension case above, this rule fires from the per-clip structural loop, which
            # both engines run *before* the fingerprint comparison -- verified by decoding the case
            # with and without a re-sign and getting `source_range_unavailable` either way.  A
            # re-sign here is inert, and describing it as one that "also compares the two
            # fingerprint implementations" was a false claim about coverage of exactly the kind this
            # item exists to remove.
            "mutations": [{"op": "replace", "path": "/clips/0/source_start_frame", "value": 72}],
            "mutation_path": "/clips/0/source_start_frame",
            "expected": _rejected_row("reject.source_range", "source_range_unavailable"),
        },
    ]
    operations = [
        {
            "case_id": "operation.accept.trim_clip",
            "operation_id": "trim_clip",
            "expected": _accepted_row(
                "operation.accept.trim_clip",
                None,
                {"operation_id": "trim_clip", "disposition": "accepted"},
            ),
        },
        {
            "case_id": "operation.reject.unknown",
            "operation_id": "plugin_operation",
            "expected": _rejected_row("operation.reject.unknown", "operation_not_in_profile"),
        },
        {
            "case_id": "operation.reject.audio_namespace",
            "operation_id": "h3.authoring.audio.command.v1.insert",
            "expected": _rejected_row("operation.reject.audio_namespace", "audio_editing_deferred"),
        },
    ]
    corpus = {
        "schema": CORPUS_SCHEMA,
        "source_fixture_sha256": _sha256(source_raw),
        "subjects": subjects,
        "mutations": mutations,
        "operations": operations,
    }
    return (json.dumps(corpus, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _pointer_parts(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise SemanticParityError("mutation path must be a JSON pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _apply_mutations(subject: object, mutations: object) -> object:
    result = copy.deepcopy(subject)
    for index, raw in enumerate(_array(mutations, "mutations")):
        mutation = _object(raw, ("op", "path", "value"), f"mutations[{index}]")
        operation, path = mutation["op"], mutation["path"]
        if operation not in ("add", "replace", "resign") or not isinstance(path, str):
            raise SemanticParityError("mutation operation is outside the closed vocabulary")
        if operation == "resign":
            # CRITICAL: each engine re-signs with its own fingerprint function, never with a value
            # carried in the corpus. That is deliberate -- a case that re-signs also compares the
            # two fingerprint implementations, because a divergence there makes one engine reject
            # the re-signed snapshot as `stale_snapshot` while the other reaches the rule the case
            # is about. Substituting a precomputed fingerprint would delete that check.
            if path != "" or mutation["value"] is not None:
                raise SemanticParityError("resign takes no path and no value")
            if not isinstance(result, dict) or "public_fingerprint" not in result:
                raise SemanticParityError("resign requires a snapshot object")
            result["public_fingerprint"] = public_snapshot_fingerprint(result)
            continue
        parts = _pointer_parts(path)
        if not parts:
            if operation != "replace":
                raise SemanticParityError("root mutation must replace")
            result = copy.deepcopy(mutation["value"])
            continue
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, list):
                parent = parent[int(part)]
            elif isinstance(parent, dict):
                parent = parent[part]
            else:
                raise SemanticParityError("mutation path traverses a scalar")
        key = parts[-1]
        if isinstance(parent, list):
            offset = int(key)
            if operation == "replace" and not 0 <= offset < len(parent):
                raise SemanticParityError("replace target is absent")
            if operation == "add":
                parent.insert(offset, copy.deepcopy(mutation["value"]))
            else:
                parent[offset] = copy.deepcopy(mutation["value"])
        elif isinstance(parent, dict):
            if operation == "replace" and key not in parent:
                raise SemanticParityError("replace target is absent")
            if operation == "add" and key in parent:
                raise SemanticParityError("add target already exists")
            parent[key] = copy.deepcopy(mutation["value"])
        else:
            raise SemanticParityError("mutation target parent is a scalar")
    return result


def _expected(row: object, case_id: str) -> dict[str, object]:
    expected = _object(row, _ROW_KEYS, f"{case_id}.expected")
    if expected["case_id"] != case_id:
        raise SemanticParityError(f"{case_id} expectation identity drifted")
    return expected


def run_python_corpus_bytes(raw: bytes) -> dict[str, object]:
    corpus = _object(
        _closed_json(raw, "semantic corpus"),
        ("schema", "source_fixture_sha256", "subjects", "mutations", "operations"),
        "semantic corpus",
    )
    if corpus["schema"] != CORPUS_SCHEMA:
        raise SemanticParityError("semantic corpus schema is unsupported")
    subjects: dict[str, dict[str, object]] = {}
    rows: list[dict[str, object]] = []
    for index, raw_subject in enumerate(_array(corpus["subjects"], "subjects")):
        subject = _object(
            raw_subject,
            ("case_id", "snapshot", "resolve_frames", "resolved_scenes", "expected"),
            f"subjects[{index}]",
        )
        case_id = subject["case_id"]
        if not isinstance(case_id, str) or case_id in subjects:
            raise SemanticParityError("subject IDs must be unique strings")
        snapshot = decode_public_snapshot(subject["snapshot"])
        frames = _array(subject["resolve_frames"], f"{case_id}.resolve_frames")
        if any(isinstance(frame, bool) or not isinstance(frame, int) for frame in frames):
            raise SemanticParityError(f"{case_id} frames must be integers")
        scenes = [resolve_composition(snapshot, cast(int, frame)) for frame in frames]
        _expected(subject["expected"], case_id)
        rows.append(
            _accepted_row(
                case_id,
                snapshot.public_fingerprint,
                _snapshot_projection(snapshot, scenes),
            )
        )
        subjects[case_id] = subject

    for index, raw_case in enumerate(_array(corpus["mutations"], "mutations")):
        case = _object(
            raw_case,
            ("case_id", "subject_id", "mutations", "mutation_path", "expected"),
            f"mutation cases[{index}]",
        )
        case_id, subject_id = case["case_id"], case["subject_id"]
        if (
            not isinstance(case_id, str)
            or not isinstance(subject_id, str)
            or subject_id not in subjects
        ):
            raise SemanticParityError("mutation case identity is invalid")
        _expected(case["expected"], case_id)
        if not isinstance(case["mutation_path"], str):
            raise SemanticParityError(f"{case_id} mutation path must be text")
        mutated = _apply_mutations(subjects[subject_id]["snapshot"], case["mutations"])
        try:
            snapshot = decode_public_snapshot(mutated)
        except CompositionContractError as exc:
            row = _rejected_row(case_id, exc.code)
        except Exception as exc:  # pragma: no cover - defensive closed boundary
            raise SemanticParityError(f"{case_id} escaped the typed rejection boundary") from exc
        else:
            row = _accepted_row(
                case_id, snapshot.public_fingerprint, _snapshot_projection(snapshot, ())
            )
        rows.append(row)

    for index, raw_case in enumerate(_array(corpus["operations"], "operations")):
        case = _object(raw_case, ("case_id", "operation_id", "expected"), f"operations[{index}]")
        case_id, operation_id = case["case_id"], case["operation_id"]
        if not isinstance(case_id, str) or not isinstance(operation_id, str):
            raise SemanticParityError("operation case identity is invalid")
        _expected(case["expected"], case_id)
        try:
            disposition = operation_disposition(operation_id)
        except CompositionContractError as exc:
            row = _rejected_row(case_id, exc.code)
        else:
            row = _accepted_row(
                case_id,
                None,
                {"operation_id": operation_id, "disposition": disposition},
            )
        rows.append(row)

    if len({cast(str, row["case_id"]) for row in rows}) != len(rows):
        raise SemanticParityError("case IDs must be globally unique")
    return {
        "schema": REPORT_SCHEMA,
        "engine": "python",
        "corpus_sha256": _sha256(raw),
        "rows": rows,
    }


def expected_rows(raw: bytes) -> list[dict[str, object]]:
    """The rows the corpus itself declares, in the order the runners emit them."""

    corpus = _object(
        _closed_json(raw, "semantic corpus"),
        ("schema", "source_fixture_sha256", "subjects", "mutations", "operations"),
        "semantic corpus",
    )
    rows: list[dict[str, object]] = []
    for section, keys in (
        ("subjects", ("case_id", "snapshot", "resolve_frames", "resolved_scenes", "expected")),
        ("mutations", ("case_id", "subject_id", "mutations", "mutation_path", "expected")),
        ("operations", ("case_id", "operation_id", "expected")),
    ):
        for index, raw_case in enumerate(_array(corpus[section], section)):
            case = _object(raw_case, keys, f"{section}[{index}]")
            case_id = case["case_id"]
            if not isinstance(case_id, str):
                raise SemanticParityError(f"{section}[{index}] identity is invalid")
            rows.append(_expected(case["expected"], case_id))
    return rows


def _rows_of(report: Mapping[str, object], name: str, engine: str) -> list[object]:
    if set(report) != {"schema", "engine", "corpus_sha256", "rows"}:
        raise SemanticParityError(f"{name} report shape is invalid")
    if report["schema"] != REPORT_SCHEMA or report["engine"] != engine:
        raise SemanticParityError(f"{name} report identity is invalid")
    rows = report["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_CASES:
        raise SemanticParityError(f"{name} report rows are invalid")
    return rows


def _diff(left: Sequence[object], right: Sequence[object], pair: str) -> None:
    if len(left) != len(right):
        raise SemanticParityError(f"{pair}: row count mismatch ({len(left)} vs {len(right)})")
    for index, (one, other) in enumerate(zip(left, right, strict=True)):
        if one == other:
            continue
        case_id = one.get("case_id", index) if isinstance(one, dict) else index
        fields = (
            sorted(key for key in set(one) | set(other) if one.get(key) != other.get(key))
            if isinstance(one, dict) and isinstance(other, dict)
            else []
        )
        raise SemanticParityError(f"{pair}: row mismatch at {case_id} in {fields or 'the row'}")


def compare_reports(
    python_report: Mapping[str, object],
    typescript_report: Mapping[str, object],
    *,
    corpus: bytes,
) -> dict[str, object]:
    """Compare both engine reports against the corpus and against each other.

    CRITICAL: neither runner asserts its own rows.  Both emit what they observed and this decides,
    which is the whole point of the item -- an earlier draft had each runner raise when its row
    disagreed with the corpus, so the comparison that followed could not fail and a planted drift
    surfaced only as a wall of runner output with no case name in it.  A runner that grows a
    self-check again silently takes this function's job away.
    """

    python_rows = _rows_of(python_report, "python", "python")
    typescript_rows = _rows_of(typescript_report, "typescript", "typescript")
    declared = expected_rows(corpus)
    if python_report["corpus_sha256"] != _sha256(corpus):
        raise SemanticParityError("python report did not read this corpus")
    if typescript_report["corpus_sha256"] != _sha256(corpus):
        raise SemanticParityError("typescript report did not read this corpus")
    _diff(python_rows, declared, "python vs corpus")
    _diff(typescript_rows, declared, "typescript vs corpus")
    _diff(python_rows, typescript_rows, "python vs typescript")
    return {
        "corpus_sha256": _sha256(corpus),
        "row_count": len(declared),
        "status": "PASS",
    }


def run_typescript_report(root: Path, corpus: Path) -> dict[str, object]:
    temp_root = root / ".tmp"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m23-46-", dir=temp_root) as directory:
        report_path = Path(directory) / "typescript-report.json"
        environment = dict(os.environ)
        environment["M23_46_CORPUS_PATH"] = str(corpus.resolve())
        environment["M23_46_REPORT_PATH"] = str(report_path.resolve())
        command = [
            "pnpm.cmd" if os.name == "nt" else "pnpm",
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "tests/semanticParity.test.ts",
            "--reporter=dot",
        ]
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if completed.returncode != 0 or not report_path.is_file():
            detail = "\n".join((completed.stdout, completed.stderr))[-4_000:]
            raise SemanticParityError(f"TypeScript runner failed closed:\n{detail}")
        report = _closed_json(report_path.read_bytes(), "TypeScript report")
    if not isinstance(report, dict):
        raise SemanticParityError("TypeScript report must be an object")
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    generate.add_argument("--output", type=Path, default=DEFAULT_CORPUS)
    generate.add_argument("--check", action="store_true")
    check = subparsers.add_parser("check")
    check.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    if args.command == "generate":
        expected = build_corpus_bytes(args.source)
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != expected:
                raise SemanticParityError("generated semantic corpus is stale")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(expected)
        print(json.dumps({"bytes": len(expected), "output": str(args.output), "status": "PASS"}))
        return 0
    raw = args.corpus.read_bytes()
    python_report = run_python_corpus_bytes(raw)
    typescript_report = run_typescript_report(ROOT, args.corpus)
    print(json.dumps(compare_reports(python_report, typescript_report, corpus=raw), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
