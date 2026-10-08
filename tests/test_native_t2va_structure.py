"""Parse-only structural admission of the native T2VA surface, on synthetic sources only."""

import ast
import hashlib
import inspect
import json

import pytest
from native_t2va_fixtures import (
    BENIGN,
    MUTATIONS,
    SYNTHETIC_DEFINITIONS,
    SYNTHETIC_SOURCE,
    mutate,
    parity_report,
    synthetic_manifest,
)

from comfyui_h3_context.core import native_t2va_structure as structure
from comfyui_h3_context.core.native_t2va_structure import (
    MAX_AST_DEPTH,
    MAX_AST_NODES,
    MAX_SOURCE_BYTES,
    NATIVE_T2VA_CLASSIFIER_SCHEMA,
    PRODUCTION_MANIFEST,
    NativeT2VAStructureVerdict,
    StructureReason,
    classify_native_t2va_source,
    definition_digest,
    derive_manifest,
    is_production_admission,
)

# Pinned under repository Python 3.13 and reproduced by the parse-only probe under the host's
# Python 3.12; a serializer change that differs by interpreter moves these values.
SYNTHETIC_MANIFEST_FINGERPRINT = (
    "sha256:6e061be06666b7c3a5427d0c04aa41f824416dd5b2c57374dcd5db77d2218ed7"
)
PARITY_REPORT_DIGEST = "sha256:099207a002691181667747a0fa1b32fcece0d3a64b33f740b90346702cc1d2cc"
# The committed production table; regenerating it is a deliberate, reviewed change.
PRODUCTION_MANIFEST_FINGERPRINT = (
    "sha256:9f37572101bc6cfd3acf41236e65d915d09ff3ce1641fbc4dd37a369e31b88c9"
)


def _classify(source: str | bytes) -> NativeT2VAStructureVerdict:
    content = source.encode("utf-8") if isinstance(source, str) else source
    return structure._classify(content, synthetic_manifest())


def _census(content: bytes) -> tuple[int, int]:
    pending: list[tuple[ast.AST, int]] = [(ast.parse(content), 1)]
    nodes = depth = 0
    while pending:
        node, level = pending.pop()
        nodes += 1
        depth = max(depth, level)
        pending.extend((child, level + 1) for child in ast.iter_child_nodes(node))
    return nodes, depth


@pytest.mark.parametrize(
    "source", ["def sample(): pass", "async def sample(): pass", "class Sample: pass"]
)
def test_missing_older_ast_type_params_matches_only_the_empty_field(source: str) -> None:
    node = ast.parse(source).body[0]
    old_fields = tuple(name for name in node._fields if name != "type_params")
    vars(node)["_fields"] = (*old_fields, "type_params")
    vars(node)["type_params"] = []
    empty_digest = definition_digest(node)
    vars(node)["_fields"] = old_fields
    del vars(node)["type_params"]
    assert definition_digest(node) == empty_digest
    vars(node)["type_params"] = [ast.Name(id="T", ctx=ast.Load())]
    assert definition_digest(node) != empty_digest
    vars(node)["_fields"] = (*old_fields, "type_params")
    assert definition_digest(node) != empty_digest
    vars(node)["type_params"] = []
    vars(node)["_fields"] = (*node._fields, "future_field")
    vars(node)["future_field"] = []
    assert definition_digest(node) != empty_digest


def test_type_params_normalization_is_not_applied_to_other_ast_types() -> None:
    node = ast.parse("value = 1").body[0]
    before = definition_digest(node)
    vars(node)["_fields"] = (*node._fields, "type_params")
    vars(node)["type_params"] = []
    assert definition_digest(node) != before


def test_synthetic_surface_is_admitted_against_its_own_manifest() -> None:
    manifest = synthetic_manifest()
    assert [name for name, _digest in manifest.definitions] == list(SYNTHETIC_DEFINITIONS)
    verdict = _classify(SYNTHETIC_SOURCE)
    assert verdict == NativeT2VAStructureVerdict(
        StructureReason.ADMITTED, "admitted", manifest.fingerprint
    )
    assert verdict.admitted and verdict.classifier_schema == NATIVE_T2VA_CLASSIFIER_SCHEMA
    assert manifest.fingerprint == SYNTHETIC_MANIFEST_FINGERPRINT


def test_production_entry_point_accepts_no_caller_table() -> None:
    # P01: a caller-supplied table can classify, but it can never produce production admission.
    assert list(inspect.signature(classify_native_t2va_source).parameters) == ["content"]
    synthetic = _classify(SYNTHETIC_SOURCE)
    assert synthetic.admitted and not is_production_admission(synthetic)
    production = classify_native_t2va_source(SYNTHETIC_SOURCE.encode("utf-8"))
    assert not production.admitted
    assert production.manifest_fingerprint == PRODUCTION_MANIFEST.fingerprint
    with pytest.raises(TypeError):
        classify_native_t2va_source(  # type: ignore[call-arg]
            SYNTHETIC_SOURCE.encode("utf-8"), synthetic_manifest()
        )


def test_production_admission_requires_the_production_manifest_and_schema() -> None:
    admitted = NativeT2VAStructureVerdict(
        StructureReason.ADMITTED, "admitted", PRODUCTION_MANIFEST.fingerprint
    )
    assert is_production_admission(admitted)
    assert not is_production_admission(
        NativeT2VAStructureVerdict(StructureReason.ADMITTED, "admitted", "sha256:" + "0" * 64)
    )
    assert not is_production_admission(
        NativeT2VAStructureVerdict(
            StructureReason.ADMITTED,
            "admitted",
            PRODUCTION_MANIFEST.fingerprint,
            classifier_schema="h3.native_t2va_structure.v0",
        )
    )
    assert not is_production_admission(
        NativeT2VAStructureVerdict(
            StructureReason.CHANGED, "definition:FPS", PRODUCTION_MANIFEST.fingerprint
        )
    )
    assert not is_production_admission({"reason": "native_t2va_structure_admitted"})


def test_production_table_holds_nine_rows_six_constants_and_hashes_only() -> None:
    names = [name for name, _digest in PRODUCTION_MANIFEST.definitions]
    assert names == [
        "align_frame_count",
        "video_latent_t",
        "temporal_shape",
        "_empty_av_latent",
        "EmptyMiniMaxH3LatentAV",
        "MiniMaxH3SigmaShift",
        "MiniMaxH3ImageToVideo.define_schema",
        "MiniMaxH3ImageToVideo.execute",
        "MiniMaxH3ImageToVideo",
    ]
    assert len(PRODUCTION_MANIFEST.constants) == 6
    digests = [digest for _name, digest in PRODUCTION_MANIFEST.definitions]
    digests += [digest for _name, digest in PRODUCTION_MANIFEST.constants]
    assert all(
        digest.startswith("sha256:") and len(digest) == 71 and int(digest[7:], 16) >= 0
        for digest in digests
    )
    assert PRODUCTION_MANIFEST.fingerprint == PRODUCTION_MANIFEST_FINGERPRINT


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_changed_surface_or_envelope_is_refused_with_a_closed_identifier(name: str) -> None:
    old, new, check = MUTATIONS[name]
    verdict = _classify(mutate(SYNTHETIC_SOURCE, old, new))
    assert verdict.reason is StructureReason.CHANGED
    assert verdict.check == check
    assert not verdict.admitted


@pytest.mark.parametrize("name", sorted(BENIGN))
def test_layout_masked_bodies_and_unrelated_additions_stay_admitted(name: str) -> None:
    old, new = BENIGN[name]
    assert _classify(mutate(SYNTHETIC_SOURCE, old, new)).admitted


def test_refusal_identifiers_never_carry_names_read_from_the_source() -> None:
    manifest = synthetic_manifest()
    closed = (
        {"admitted", "unsupported_syntax"}
        | {"definition:" + name for name, _digest in manifest.definitions}
        | {"definition:" + name.split(".", 1)[0] for name, _digest in manifest.definitions}
        | {"constant:" + name for name, _digest in manifest.constants}
        | {"binding:" + name for name in manifest.protected_names()}
        | {"import:" + name for name in manifest.required_imports}
        | {"guard:" + structure.IMAGE_TO_VIDEO}
        | {
            "envelope_" + suffix
            for suffix in (
                "function",
                "class",
                "duplicate",
                "dunder",
                "alias",
                "import",
                "assign",
                "statement",
                "global",
            )
        }
    )
    for old, new, _check in MUTATIONS.values():
        assert _classify(mutate(SYNTHETIC_SOURCE, old, new)).check in closed


def test_canonical_form_keeps_scalar_types_order_and_emptiness_distinct() -> None:
    def digest(source: str) -> str:
        return definition_digest(ast.parse(source).body[0])

    distinct = [
        "x = None",
        "x = []",
        "x = ()",
        "x = True",
        "x = 1",
        "x = 1.0",
        "x = -0.0",
        "x = 0.0",
        "x = 'a'",
        "x = b'a'",
        "x = [1, 2]",
        "x = [2, 1]",
    ]
    assert len({digest(source) for source in distinct}) == len(distinct)
    # Location attributes are not part of the digest.
    assert digest("x = [1, 2]") == definition_digest(ast.parse("\n\n\nx   =   [1,\n 2]").body[0])


def test_unknown_nodes_are_distinct_and_unknown_values_are_refused() -> None:
    class Foreign(ast.expr):
        _fields = ()

    assert structure._canonical(Foreign()) != structure._canonical(ast.Pass())
    with pytest.raises(structure._Refusal) as refusal:
        structure._canonical(ast.Constant(value=object()))  # type: ignore[arg-type]
    assert refusal.value.check == "unsupported_syntax"


@pytest.mark.parametrize(
    "content,check",
    [
        ("text, not bytes", "source_type"),
        (b"# -*- coding: not-a-codec -*-\n", "source_encoding"),
        (b"x = '\xff\xfe'\n", "source_encoding"),
        (b"def broken(:\n", "source_syntax"),
        (b"x = 1\x00\n", "source_syntax"),
        (b"x = " + b"(" * 400 + b"1" + b")" * 400 + b"\n", None),
    ],
)
def test_malformed_sources_fail_closed_without_raising(content: object, check: str | None) -> None:
    verdict = structure._classify(content, synthetic_manifest())  # type: ignore[arg-type]
    assert verdict.reason is StructureReason.UNPARSABLE
    if check is not None:
        assert verdict.check == check
    else:
        assert verdict.check in {"source_syntax", "source_depth_limit"}


def test_declared_encodings_are_honored() -> None:
    latin = ("# -*- coding: latin-1 -*-\n# caf\xe9\n" + SYNTHETIC_SOURCE).encode("latin-1")
    assert _classify(latin).admitted
    assert _classify(b"\xef\xbb\xbf" + SYNTHETIC_SOURCE.encode("utf-8")).admitted


def test_line_endings_and_form_feeds_carry_no_structure() -> None:
    # Standard tokenizer layout: CRLF endings and a line-leading form feed change no digest.
    assert _classify(SYNTHETIC_SOURCE.replace("\n", "\r\n").encode("utf-8")).admitted
    form_feed = mutate(SYNTHETIC_SOURCE, "\ndef align_frame_count", "\n\x0cdef align_frame_count")
    assert _classify(form_feed.encode("utf-8")).admitted


def test_source_size_bound_is_inclusive() -> None:
    content = SYNTHETIC_SOURCE.encode("utf-8")
    at_limit = content + b"#" + b"x" * (MAX_SOURCE_BYTES - len(content) - 2) + b"\n"
    assert len(at_limit) == MAX_SOURCE_BYTES
    assert _classify(at_limit).admitted
    verdict = _classify(at_limit + b"\n")
    assert (verdict.reason, verdict.check) == (StructureReason.UNPARSABLE, "source_size")


def test_ast_node_bound_is_inclusive_and_counts_every_occurrence() -> None:
    content = SYNTHETIC_SOURCE.encode("utf-8")
    base, _depth = _census(content)
    # `PAD = (0, ..., 0)` adds Assign, Name, Store, Tuple and Load plus one Constant per item.
    items = MAX_AST_NODES - base - 5
    at_limit = content + b"PAD = (" + b"0, " * items + b")\n"
    assert _census(at_limit)[0] == MAX_AST_NODES
    assert _classify(at_limit).admitted
    over = content + b"PAD = (" + b"0, " * (items + 1) + b")\n"
    assert _census(over)[0] == MAX_AST_NODES + 1
    verdict = _classify(over)
    assert (verdict.reason, verdict.check) == (StructureReason.UNPARSABLE, "source_node_limit")


def test_ast_depth_bound_is_inclusive() -> None:
    content = SYNTHETIC_SOURCE.encode("utf-8")

    def deep(count: int) -> bytes:
        return content + b"DEEP = " + b"-" * count + b"1\n"

    count = next(n for n in range(1, MAX_AST_DEPTH) if _census(deep(n))[1] == MAX_AST_DEPTH)
    assert _classify(deep(count)).admitted
    assert _census(deep(count + 1))[1] == MAX_AST_DEPTH + 1
    verdict = _classify(deep(count + 1))
    assert (verdict.reason, verdict.check) == (StructureReason.UNPARSABLE, "source_depth_limit")


def test_derivation_refuses_an_open_closure_or_a_missing_definition() -> None:
    open_closure = mutate(
        SYNTHETIC_SOURCE,
        "    return max(1, round(frames / 4)) * 4 + 1\n",
        "    return max(1, round(frames / 4)) * 4 + helper()\n\n\ndef helper():\n    return 1\n",
    )
    with pytest.raises(ValueError, match="unselected module binding"):
        derive_manifest(
            open_closure.encode("utf-8"),
            source_revision="synthetic",
            source_blob="0" * 40,
            definitions=SYNTHETIC_DEFINITIONS,
        )
    with pytest.raises(ValueError, match="definition absent"):
        derive_manifest(
            SYNTHETIC_SOURCE.encode("utf-8"),
            source_revision="synthetic",
            source_blob="0" * 40,
            definitions=(*SYNTHETIC_DEFINITIONS, "absent_helper"),
        )
    with pytest.raises(ValueError, match="top-level definition row"):
        derive_manifest(
            SYNTHETIC_SOURCE.encode("utf-8"),
            source_revision="synthetic",
            source_blob="0" * 40,
            definitions=("MiniMaxH3ImageToVideo.execute",),
        )


def test_parity_report_is_pinned_for_the_cross_interpreter_probe() -> None:
    # P09/P32: the probe prints this digest under the host interpreter; equality is the parity.
    report = parity_report()
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert "sha256:" + hashlib.sha256(encoded).hexdigest() == PARITY_REPORT_DIGEST
