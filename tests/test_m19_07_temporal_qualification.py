"""Focused tests for the M19-07 pre-M20 qualification harness.

The tests deliberately run against synthetic source fixtures rather than the pinned host file. A
harness that only ever sees one input cannot demonstrate that it derives anything: it might be
agreeing with a value someone typed into it. Feeding it a different, made-up subject with different
rates proves the derivation is real, and feeding it a degenerate one proves the harness notices when
its own candidate set stops separating the decisions it is supposed to separate.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from typing import Any

import pytest

from scripts.m19_07.evidence import (
    REQUIRED_ROWS,
    Exhaustiveness,
    RedactionError,
    Row,
    RowStatus,
    assert_redacted,
    build_matrix,
    enforce_status_rules,
)
from scripts.m19_07.purity import (
    ImpureSourceError,
    LoopBudgetExceeded,
    assert_pure,
    compile_pure_functions,
    safe_constant,
)
from scripts.m19_07.source_facts import (
    SourceFactError,
    SourceFacts,
    SourceIdentity,
    derive,
    git_blob_id,
)
from scripts.m19_07.temporal import (
    candidate_set_separates_decisions,
    describe_audio_join,
    lattice_members,
    measure_candidates,
    off_grid_rejected,
)

# --------------------------------------------------------------------------------------
# Synthetic subjects. Neither is the real pinned source; both are shaped like it.
# --------------------------------------------------------------------------------------

_RATIONAL_SUBJECT = '''
"""A made-up subject whose audio rate does not divide its video rate."""

import torch

FPS = 30
AUDIO_LATENT_FPS = 40
CANVAS_MULTIPLE = 16
BASE_SHORT_EDGE = 512
MAX_PIXELS = 512 * 512
REF_IMAGE_SHORT_EDGE = 1024


def align_frame_count(n):
    while n % 7 != 3:
        n += 1
    return n


def video_latent_t(frame_count):
    return 1 if frame_count <= 3 else ((frame_count - 3) // 7) * 2 + 1


def temporal_shape(length):
    frame_count = align_frame_count(max(3, length))
    duration = frame_count / FPS
    return frame_count, video_latent_t(frame_count), round(duration * AUDIO_LATENT_FPS)


def _empty_av_latent(width, height, length, batch_size=1):
    frame_count, latent_t, audio_t = temporal_shape(length)
    video = torch.zeros([batch_size, 8, latent_t, height // 8, width // 8])
    audio = torch.zeros([batch_size, 16, 2, audio_t])
    return {"samples": (video, audio)}, frame_count


class ExampleLatentNode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ExampleLatentAV",
            inputs=[
                io.Int.Input("width", default=512),
                io.Int.Input("length", default=31),
                io.Image.Input("first_frame", optional=True),
            ],
        )
'''

#: Same shape, but the audio rate divides the video rate, so every frame count is exact and the
#: rounded-join decision becomes invisible.
_DEGENERATE_SUBJECT = _RATIONAL_SUBJECT.replace("AUDIO_LATENT_FPS = 40", "AUDIO_LATENT_FPS = 60")


def _identity(text: str) -> SourceIdentity:
    data = text.encode("utf-8")
    return SourceIdentity(
        label="synthetic/subject.py",
        blob=f"gitblob:{git_blob_id(data)}",
        sha256="sha256:" + "0" * 64,
        byte_length=len(data),
    )


@pytest.fixture
def rational_facts() -> SourceFacts:
    return derive(_RATIONAL_SUBJECT, _identity(_RATIONAL_SUBJECT))


# --------------------------------------------------------------------------------------
# Purity gate
# --------------------------------------------------------------------------------------


def _fn(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _expr(source: str) -> ast.expr:
    """The right-hand side of a single assignment, typed as an expression.

    `ast.parse(...).body[0]` is an `ast.stmt`, and reading `.value` off it is only sound once the
    statement is known to be an assignment. Asserting that here keeps the constant-evaluation tests
    honest under strict typing instead of silencing the checker at each call site.
    """

    node = ast.parse(source).body[0]
    assert isinstance(node, ast.Assign)
    return node.value


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    import math\n    return x",
        "def f(x):\n    return math.ceil(x)",
        "def f(x):\n    return x[0]",
        "def f(x):\n    return (lambda y: y)(x)",
        "def f(x):\n    return [y for y in range(x)]",
        "def f(x):\n    try:\n        return x\n    except ValueError:\n        return 0",
        "def f(x=1):\n    return x",
    ],
    ids=["import", "attribute-call", "subscript", "lambda", "comprehension", "try", "default"],
)
def test_purity_gate_rejects_constructs_outside_the_subset(source: str) -> None:
    with pytest.raises(ImpureSourceError):
        assert_pure(_fn(source), label="f")


def test_purity_gate_admits_the_pinned_subset_and_declared_siblings() -> None:
    definition = _fn("def outer(n):\n    return inner(max(5, n)) + round(1.5)")
    assert_pure(definition, label="outer", allowed_calls=frozenset({"inner"}))


def test_compiled_functions_evaluate_the_subjects_own_arithmetic(
    rational_facts: SourceFacts,
) -> None:
    # 31 is the first member of the synthetic 7k+3 lattice at or above 30.
    assert rational_facts.functions["align_frame_count"](30) == 31
    assert rational_facts.functions["temporal_shape"](30)[0] == 31


def test_loop_guard_turns_a_non_terminating_source_into_a_loud_failure() -> None:
    definition = _fn("def spin(n):\n    while n != -1:\n        n += 1\n    return n")
    compiled = compile_pure_functions({"spin": definition}, constants={})
    with pytest.raises(LoopBudgetExceeded):
        compiled["spin"](0)


def test_safe_constant_evaluates_arithmetic_but_refuses_calls() -> None:
    assert safe_constant(_expr("VALUE = 768 * 1344"), known={}, label="VALUE") == 1032192
    call = _expr("VALUE = int(3.2)")
    with pytest.raises(ImpureSourceError):
        safe_constant(call, known={}, label="VALUE")


# --------------------------------------------------------------------------------------
# Source derivation
# --------------------------------------------------------------------------------------


def test_derivation_reads_rates_from_the_subject_not_from_the_harness(
    rational_facts: SourceFacts,
) -> None:
    assert rational_facts.constants["FPS"] == 30
    assert rational_facts.constants["AUDIO_LATENT_FPS"] == 40
    assert rational_facts.constants["MAX_PIXELS"] == 512 * 512


def test_derivation_reads_the_joint_latent_descriptor_structurally(
    rational_facts: SourceFacts,
) -> None:
    video = rational_facts.stream("video")
    audio = rational_facts.stream("audio")
    # `stream` answers `None` for a name the subject never declares. Asserting presence first is
    # part of the test: a subject missing either half of the pair is a failure, not a skip.
    assert video is not None and audio is not None
    assert (video.channels, video.rank) == (8, 5)
    assert (audio.channels, audio.rank) == (16, 4)
    assert "height // 8" in video.dims


def test_derivation_reads_node_schemas_including_optionality(rational_facts: SourceFacts) -> None:
    node = rational_facts.node("ExampleLatentAV")
    assert node is not None
    assert node.input_names() == ("width", "length", "first_frame")
    assert [item.optional for item in node.inputs] == [False, False, True]


def test_a_subject_missing_a_required_rate_fails_rather_than_falling_back() -> None:
    stripped = _RATIONAL_SUBJECT.replace("AUDIO_LATENT_FPS = 40\n", "")
    with pytest.raises(SourceFactError, match="AUDIO_LATENT_FPS"):
        derive(stripped, _identity(stripped))


def test_a_documented_claim_cannot_stand_in_for_a_declaration() -> None:
    """A rate mentioned only in prose is not a declaration and must not be picked up."""
    commented = _RATIONAL_SUBJECT.replace(
        "AUDIO_LATENT_FPS = 40",
        "# The reference implementation uses AUDIO_LATENT_FPS = 40 (community report)",
    )
    with pytest.raises(SourceFactError, match="AUDIO_LATENT_FPS"):
        derive(commented, _identity(commented))


# --------------------------------------------------------------------------------------
# Temporal separation
# --------------------------------------------------------------------------------------


def test_lattice_and_join_are_derived_for_an_unfamiliar_subject(
    rational_facts: SourceFacts,
) -> None:
    members = lattice_members(rational_facts, lower=3, upper=60)
    assert members[:4] == (3, 10, 17, 24)
    join = describe_audio_join(rational_facts, members)
    assert join.ratio == "4/3"
    assert join.is_rounded_join is True
    assert join.float_matches_exact is True


def test_candidate_set_must_actually_separate_the_three_decisions(
    rational_facts: SourceFacts,
) -> None:
    ok, reason = candidate_set_separates_decisions(measure_candidates(rational_facts, (3, 10, 24)))
    assert ok, reason


def test_a_degenerate_subject_is_reported_rather_than_silently_accepted() -> None:
    facts = derive(_DEGENERATE_SUBJECT, _identity(_DEGENERATE_SUBJECT))
    ok, reason = candidate_set_separates_decisions(measure_candidates(facts, (3, 10, 24)))
    assert not ok
    assert "rounded audio join" in reason


def test_off_grid_values_are_snapped_upward_never_accepted(rational_facts: SourceFacts) -> None:
    align = rational_facts.functions["align_frame_count"]
    assert align(11) == 17
    ok, problems = off_grid_rejected(rational_facts)
    # The real subject's off-grid samples are not on this synthetic lattice either, so the
    # helper's own contract still holds: nothing is ever snapped downward.
    assert ok, problems


# --------------------------------------------------------------------------------------
# Status rules
# --------------------------------------------------------------------------------------


def _row(**overrides: Any) -> Row:
    base: dict[str, Any] = {
        "name": "temporal_profile",
        "status": RowStatus.SUPPORTED,
        "reason_code": "derived",
        "consumer": "M20-01",
        "source_evidence": {"checked": 1},
    }
    base.update(overrides)
    return Row(**base)


def test_supported_without_live_evidence_or_a_reason_is_downgraded() -> None:
    result = enforce_status_rules(_row())
    assert result.status is RowStatus.UNQUALIFIED
    assert result.reason_code == "supported_without_basis"


def test_supported_survives_on_a_recorded_non_applicability_reason() -> None:
    result = enforce_status_rules(_row(live_applicability="computed before any weight loads"))
    assert result.status is RowStatus.SUPPORTED


def test_supported_survives_on_live_evidence() -> None:
    result = enforce_status_rules(_row(live_evidence={"latent_shape": [1, 24, 37, 16, 24]}))
    assert result.status is RowStatus.SUPPORTED


def test_unsupported_without_exhaustiveness_is_downgraded() -> None:
    result = enforce_status_rules(_row(status=RowStatus.UNSUPPORTED))
    assert result.status is RowStatus.UNQUALIFIED
    assert result.reason_code == "unsupported_without_exhaustiveness"


def test_unsupported_with_a_partial_exhaustiveness_record_is_still_downgraded() -> None:
    partial = Exhaustiveness(
        source_identity="gitblob:" + "a" * 40,
        candidate_mechanisms=("mask",),
        live_node_inventory=(),
        inventory_source="",
    )
    result = enforce_status_rules(_row(status=RowStatus.UNSUPPORTED, exhaustiveness=partial))
    assert result.status is RowStatus.UNQUALIFIED


def test_unsupported_survives_a_complete_exhaustiveness_record() -> None:
    complete = Exhaustiveness(
        source_identity="gitblob:" + "a" * 40,
        candidate_mechanisms=("mask", "noise_mask"),
        live_node_inventory=("SetLatentNoiseMask",),
        inventory_source="supplied host /object_info",
    )
    result = enforce_status_rules(_row(status=RowStatus.UNSUPPORTED, exhaustiveness=complete))
    assert result.status is RowStatus.UNSUPPORTED


# --------------------------------------------------------------------------------------
# Privacy and matrix closure
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"root": "B:\\projects\\ComfyUI"},
        {"root": "/home/ray/ComfyUI"},
        {"url": "file:///models/weight.safetensors"},
        {"url": "https://example.invalid/models"},
        {"note": "api_key = abc123"},
        # The userinfo form is the shape the redaction rule must refuse, so the literal has to
        # stay in the fixture; it names no real host and authenticates nothing.
        {"url": "http://user:pass@127.0.0.1:8188/"},  # pragma: allowlist secret
        {"share": "\\\\fileserver\\models"},
    ],
    ids=["windows", "posix-home", "file-url", "remote-url", "credential", "userinfo", "unc"],
)
def test_private_material_cannot_reach_an_evidence_record(payload: dict[str, str]) -> None:
    with pytest.raises(RedactionError):
        assert_redacted(payload)


def test_the_supplied_loopback_endpoint_is_allowed() -> None:
    assert_redacted({"host": "http://127.0.0.1:8188/", "weight": "weight:0123456789abcdef"})


def _closed_rows() -> list[Row]:
    return [
        Row(
            name=name,
            status=RowStatus.UNQUALIFIED,
            reason_code="pending",
            consumer="M20-01",
            source_evidence={},
        )
        for name in REQUIRED_ROWS
    ]


def test_the_matrix_is_closed_over_exactly_the_five_rows() -> None:
    matrix = build_matrix(subject={"item": "M19-07"}, rows=_closed_rows())
    assert [row["row"] for row in matrix["rows"]] == list(REQUIRED_ROWS)


def test_a_missing_row_is_refused() -> None:
    with pytest.raises(ValueError, match="missing required rows"):
        build_matrix(subject={}, rows=_closed_rows()[:-1])


def test_an_invented_row_is_refused() -> None:
    rows = _closed_rows()
    rows.append(
        Row(
            name="speculative_capability",
            status=RowStatus.SUPPORTED,
            reason_code="wishful",
            consumer="M20-99",
            source_evidence={},
            live_applicability="none",
        )
    )
    with pytest.raises(ValueError, match="outside the closed set"):
        build_matrix(subject={}, rows=rows)


def test_the_matrix_refuses_to_emit_a_private_path_even_in_the_subject() -> None:
    with pytest.raises(RedactionError):
        build_matrix(subject={"host_root": "A:\\ComfyUI"}, rows=_closed_rows())


# --------------------------------------------------------------------------------------
# Schema conformance
# --------------------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "m19_07"
    raw = (path / "qualification_evidence_v1.schema.json").read_text(encoding="utf-8")
    schema = json.loads(raw)
    assert isinstance(schema, dict)
    return schema


def _subject() -> dict[str, Any]:
    return {
        "item": "M19-07",
        "project_commit": "0" * 40,
        "native_source": {
            "label": "comfy_extras/nodes_minimax_h3.py",
            "blob": "gitblob:" + "b" * 40,
            "sha256": "sha256:" + "c" * 64,
            "byte_length": 15728,
        },
        "frozen_host_version": "0.32.0",
        "frozen_host_revision": "d" * 40,
        "model_subject": ["weight:0123456789abcdef"],
        "host": None,
        "host_state": "not_probed",
    }


def test_the_emitted_matrix_conforms_to_the_closed_schema() -> None:
    import jsonschema

    matrix = build_matrix(subject=_subject(), rows=_closed_rows(), limitations=["offline stage"])
    jsonschema.validate(matrix, _schema())


def test_the_schema_itself_refuses_a_supported_row_without_a_basis() -> None:
    import jsonschema

    matrix = build_matrix(subject=_subject(), rows=_closed_rows())
    matrix["rows"][0]["status"] = "supported"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(matrix, _schema())


def test_the_schema_itself_refuses_an_unsupported_row_without_exhaustiveness() -> None:
    import jsonschema

    matrix = build_matrix(subject=_subject(), rows=_closed_rows())
    matrix["rows"][0]["status"] = "unsupported"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(matrix, _schema())


def test_the_schema_refuses_a_model_locator_recorded_in_full() -> None:
    import jsonschema

    subject = _subject()
    subject["model_subject"] = ["minimax_h3_fl2va_bf16.safetensors"]
    matrix = build_matrix(subject=subject, rows=_closed_rows())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(matrix, _schema())


# --------------------------------------------------------------------------------------
# An unread inventory must never read as a confirmed absence
# --------------------------------------------------------------------------------------


def _finding_text(rows: Sequence[Row], name: str) -> str:
    row = next(item for item in rows if item.name == name)
    return " ".join(row.findings)


def test_an_unread_inventory_is_reported_as_unread_not_as_nothing_found(
    rational_facts: SourceFacts,
) -> None:
    from scripts.m19_07.rows import build_rows

    rows = build_rows(
        rational_facts,
        inventory_candidates={"dual_domain_av_mask": []},
        inventory_available=False,
    )
    text = _finding_text(rows, "dual_domain_av_mask")
    assert "was not read on this run" in text
    assert "no absence claim about the wider host is possible" in text


def test_a_read_inventory_with_no_hits_says_so_distinctly(rational_facts: SourceFacts) -> None:
    from scripts.m19_07.rows import build_rows

    rows = build_rows(
        rational_facts,
        inventory_candidates={"dual_domain_av_mask": []},
        inventory_available=True,
    )
    text = _finding_text(rows, "dual_domain_av_mask")
    assert "was read and contains no generic candidate" in text
    assert "was not read on this run" not in text


def test_a_read_inventory_with_hits_records_the_absence_is_not_yet_exhaustive(
    rational_facts: SourceFacts,
) -> None:
    from scripts.m19_07.rows import build_rows

    rows = build_rows(
        rational_facts,
        inventory_candidates={"dual_domain_av_mask": ["SetLatentNoiseMask"]},
        inventory_available=True,
    )
    text = _finding_text(rows, "dual_domain_av_mask")
    assert "not yet exhaustive" in text


# --------------------------------------------------------------------------------------
# A probed failure and an unattempted mechanism are different results
# --------------------------------------------------------------------------------------


def _rows_with(rational_facts: SourceFacts, **live: Any) -> tuple[Row, ...]:
    from scripts.m19_07.rows import build_rows

    return build_rows(
        rational_facts,
        inventory_candidates={},
        inventory_available=True,
        live=live or None,
    )


def test_a_diverged_probe_is_not_reported_as_unattempted(rational_facts: SourceFacts) -> None:
    rows = _rows_with(
        rational_facts,
        probes=[
            {"mechanism": "completed_boundary_resume", "node_type": "X", "outcome": "diverged"}
        ],
    )
    row = next(r for r in rows if r.name == "completed_boundary_resume")
    assert row.reason_code == "live_probe_diverged"


def test_an_unattempted_mechanism_says_so(rational_facts: SourceFacts) -> None:
    rows = _rows_with(rational_facts)
    row = next(r for r in rows if r.name == "two_ended_av_bridge")
    assert row.reason_code == "not_probed_composition_unmeasured"


def _mask_verdict(**overrides: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "status": "unsupported",
        "reason_code": "mask_carrier_reaches_one_domain_only",
        "candidate_mechanisms": ["mask input on any native node"],
        "live_node_inventory": ["SetLatentNoiseMask", "SolidMask"],
        "inventory_source": "supplied host /object_info",
        "findings": ["measured: the mask reaches one domain only"],
    }
    verdict.update(overrides)
    return verdict


def _mask_row(facts: SourceFacts, candidates: list[str], verdict: dict[str, Any]) -> Row:
    from scripts.m19_07.rows import build_rows

    rows = build_rows(
        facts,
        inventory_candidates={"dual_domain_av_mask": candidates},
        inventory_available=True,
        live={"verdicts": {"dual_domain_av_mask": verdict}},
    )
    return next(r for r in rows if r.name == "dual_domain_av_mask")


def test_a_measured_absence_becomes_unsupported_when_it_accounts_for_the_search(
    rational_facts: SourceFacts,
) -> None:
    row = _mask_row(rational_facts, ["SetLatentNoiseMask", "SolidMask"], _mask_verdict())
    assert row.status is RowStatus.UNSUPPORTED
    assert row.exhaustiveness is not None and row.exhaustiveness.is_complete()
    assert enforce_status_rules(row).status is RowStatus.UNSUPPORTED


def test_an_absence_claim_narrower_than_the_search_is_downgraded(
    rational_facts: SourceFacts,
) -> None:
    """The defect a distinct review caught: a structurally complete record that is not consistent.

    The shipped row named six nodes while the harness's own search had returned 361, four of them
    named for the very mechanism the row declared absent. Every field was populated, so the
    completeness check passed it. Consistency is a separate property and is now enforced.
    """
    row = _mask_row(
        rational_facts,
        ["SetLatentNoiseMask", "SolidMask", "LTXVAudioVideoMask", "CreateAudioMask"],
        _mask_verdict(),
    )
    assert row.status is RowStatus.UNQUALIFIED
    assert row.reason_code == "unsupported_claim_outran_the_search"
    assert "LTXVAudioVideoMask" in " ".join(row.findings)


def test_an_absence_claim_may_rule_a_candidate_out_explicitly(rational_facts: SourceFacts) -> None:
    row = _mask_row(
        rational_facts,
        ["SetLatentNoiseMask", "SolidMask", "LTXVAudioVideoMask"],
        _mask_verdict(ruled_out=["LTXVAudioVideoMask"]),
    )
    assert row.status is RowStatus.UNSUPPORTED


def test_an_unsupported_verdict_without_inventory_cannot_survive(
    rational_facts: SourceFacts,
) -> None:
    rows = _rows_with(
        rational_facts,
        verdicts={
            "dual_domain_av_mask": {
                "status": "unsupported",
                "reason_code": "asserted_without_search",
                "candidate_mechanisms": ["something"],
                "live_node_inventory": [],
                "inventory_source": "",
                "findings": [],
            }
        },
    )
    row = next(r for r in rows if r.name == "dual_domain_av_mask")
    assert enforce_status_rules(row).status is RowStatus.UNQUALIFIED


# --------------------------------------------------------------------------------------
# Gaps a distinct review found in the purity gate and the redaction rule
# --------------------------------------------------------------------------------------


def test_a_bare_name_read_outside_the_declared_scope_is_refused() -> None:
    """Restricting only call targets left `__builtins__` readable as a plain name."""
    with pytest.raises(ImpureSourceError, match="undeclared name"):
        assert_pure(_fn("def leak():\n    return __builtins__"), label="leak")


def test_parameters_and_assignments_remain_readable() -> None:
    assert_pure(_fn("def f(n):\n    m = n + 1\n    return m"), label="f")


def test_an_unbounded_exponent_is_refused() -> None:
    """`**` turns a short expression into an unbounded computation. Loops were guarded; this
    was not."""
    with pytest.raises(ImpureSourceError, match="exceeds"):
        assert_pure(_fn("def bomb(x):\n    return x ** 200000"), label="bomb")


def test_a_computed_exponent_is_refused() -> None:
    with pytest.raises(ImpureSourceError, match="integer literal"):
        assert_pure(_fn("def bomb(x):\n    return x ** x"), label="bomb")


def test_a_small_literal_exponent_is_still_allowed() -> None:
    assert_pure(_fn("def square(x):\n    return x ** 2"), label="square")


@pytest.mark.parametrize(
    "leak",
    [
        "/b/projects/ComfyUI/private/weights.safetensors",
        "/data/models/h3/",
        "/srv/comfy/output/",
        "/opt/comfyui/models/",
        "//fileserver/share/models/",
    ],
    ids=["msys-drive", "data", "srv", "opt", "forward-slash-unc"],
)
def test_absolute_path_shapes_the_old_blocklist_missed_are_refused(leak: str) -> None:
    with pytest.raises(RedactionError):
        assert_redacted({"v": leak})


@pytest.mark.parametrize(
    "kept",
    [
        "http://127.0.0.1:8188/",
        "comfy_extras/nodes_minimax_h3.py",
        "5/3",
        "37/40",
        "supplied host /object_info, 6349 node classes",
    ],
    ids=["loopback", "relative-source", "ratio", "extent", "route-mention"],
)
def test_the_inverted_rule_still_admits_legitimate_evidence(kept: str) -> None:
    assert_redacted({"v": kept})
