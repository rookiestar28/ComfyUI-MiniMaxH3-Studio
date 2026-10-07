"""M23-46: one corpus, two engines, and a comparator that belongs to neither.

The corpus is generated from the accepted M25-10 fixture and both engines run it and report what
they observed. Nothing here compares exception prose: a rejection is compared by its stable blocker
code, so either implementation stays free to reword a diagnostic.

CRITICAL: this item installs a drift guard while the two sides already agree, so passing is the
expected state and a corpus that compared nothing would look exactly like one that works. The
evidence that distinguishes them is a planted one-sided drift, which is why the mutation tests below
exist and why neither runner is allowed to check its own rows.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest

from comfyui_h3_context.core.composition_contract import (
    BLOCKER_CODES,
    CompositionContractError,
    decode_public_snapshot,
)
from scripts.semantic_parity import (
    SemanticParityError,
    _apply_mutations,
    _closed_json,
    build_corpus_bytes,
    compare_reports,
    expected_rows,
    run_python_corpus_bytes,
    run_typescript_report,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "fixtures" / "m25_10_composition_contract_v1.json"
CORPUS = ROOT / "tests" / "fixtures" / "m23_46_semantic_parity_v1.json"


def rows_of(report: dict[str, object]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", report["rows"])


def test_the_corpus_is_generated_and_never_a_second_authority() -> None:
    # Regenerated from the accepted fixture and compared byte for byte, so the corpus cannot drift
    # into being an independent statement of the contract that outlives the code it describes.
    assert build_corpus_bytes(SOURCE) == CORPUS.read_bytes()


def test_the_python_engine_reaches_every_rule_the_corpus_names() -> None:
    raw = CORPUS.read_bytes()
    report = run_python_corpus_bytes(raw)
    assert report["schema"] == "h3.context.semantic_parity_report.v1"
    assert report["engine"] == "python"
    rows = rows_of(report)
    assert len(rows) == len(expected_rows(raw)) >= 12
    assert all(set(row) == set(expected_rows(raw)[0]) for row in rows)
    assert all("message" not in row for row in rows)
    codes = {row["code"] for row in rows if row["status"] == "rejected"}
    # The two the first draft never reached: both rules sit behind the public fingerprint check, so
    # an unsigned mutation is refused as `stale_snapshot` long before the rule runs. Their cases
    # re-sign, and this asserts they arrive rather than trusting the corpus text.
    assert {"audio_editing_deferred", "source_range_unavailable"} <= codes
    assert "stale_snapshot" in codes  # and the unsigned case still reports staleness


def test_a_one_sided_drift_is_named_by_case_rather_than_swallowed() -> None:
    # The comparator's own guarantee, checked on synthetic reports: each direction fails, and the
    # message identifies the case and the field so a real drift is diagnosable from the gate log.
    raw = CORPUS.read_bytes()
    python_report = run_python_corpus_bytes(raw)
    typescript_report = copy.deepcopy(python_report)
    typescript_report["engine"] = "typescript"
    assert compare_reports(python_report, typescript_report, corpus=raw)["status"] == "PASS"

    python_drift = copy.deepcopy(python_report)
    rows_of(python_drift)[0]["projection"]["output"]["duration_frames"] += 1
    with pytest.raises(SemanticParityError, match="python vs corpus: row mismatch at accept"):
        compare_reports(python_drift, typescript_report, corpus=raw)

    typescript_drift = copy.deepcopy(typescript_report)
    rows_of(typescript_drift)[-1]["code"] = "invalid_contract"
    with pytest.raises(
        SemanticParityError, match="typescript vs corpus: row mismatch at operation"
    ):
        compare_reports(python_report, typescript_drift, corpus=raw)


def test_a_report_from_another_corpus_is_refused() -> None:
    raw = CORPUS.read_bytes()
    python_report = run_python_corpus_bytes(raw)
    typescript_report = copy.deepcopy(python_report)
    typescript_report["engine"] = "typescript"
    typescript_report["corpus_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(SemanticParityError, match="did not read this corpus"):
        compare_reports(python_report, typescript_report, corpus=raw)


def test_both_engines_agree_on_the_same_corpus_bytes() -> None:
    # The differential itself. The TypeScript runner is invoked through the repository's pinned
    # Vitest, so the ordinary gate exercises this seam; its absence fails closed and never skips.
    raw = CORPUS.read_bytes()
    python_report = run_python_corpus_bytes(raw)
    typescript_report = run_typescript_report(ROOT, CORPUS)
    assert compare_reports(python_report, typescript_report, corpus=raw) == {
        "corpus_sha256": python_report["corpus_sha256"],
        "row_count": len(rows_of(python_report)),
        "status": "PASS",
    }


def _mutation_case(case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    corpus = cast("dict[str, Any]", _closed_json(CORPUS.read_bytes(), "corpus"))
    subjects = {row["case_id"]: row for row in corpus["subjects"]}
    case = next(row for row in corpus["mutations"] if row["case_id"] == case_id)
    return case, subjects[case["subject_id"]]


def _decode_code(subject: dict[str, Any], mutations: list[dict[str, Any]]) -> str:
    try:
        decode_public_snapshot(_apply_mutations(subject["snapshot"], mutations))
    except CompositionContractError as error:
        return error.code
    return "accepted"


def test_only_the_case_that_needs_a_resign_carries_one() -> None:
    """Pin which mutation cases the fingerprint check would otherwise mask, and which it would not.

    CRITICAL: a `resign` that changes nothing is not harmless. An earlier revision of this
    corpus carried one on `reject.source_range` and described it as also comparing the two
    fingerprint implementations, which was a false claim about coverage -- the same defect class
    this whole item exists to remove, written into the item's own fixture. This asserts the
    distinction behaviourally rather than trusting either comment.
    """

    audio, audio_subject = _mutation_case("reject.independent_audio_members")
    unsigned = [row for row in audio["mutations"] if row["op"] != "resign"]
    assert len(unsigned) < len(audio["mutations"]), "the audio case must re-sign"
    # Without the re-sign the fingerprint check fires first and the rule is never reached.
    assert _decode_code(audio_subject, unsigned) == "stale_snapshot"
    assert _decode_code(audio_subject, audio["mutations"]) == "audio_editing_deferred"

    source_range, range_subject = _mutation_case("reject.source_range")
    assert all(row["op"] != "resign" for row in source_range["mutations"]), (
        "reject.source_range must not re-sign: its rule fires from the per-clip structural loop, "
        "which both engines run before the fingerprint comparison"
    )
    assert _decode_code(range_subject, source_range["mutations"]) == "source_range_unavailable"


def test_an_unlisted_rejection_code_collapses_to_invalid_contract() -> None:
    # The Python half of a property the TypeScript codec now mirrors: `.code` is always a member of
    # the shared vocabulary, so a reader that switches on it cannot meet a case it has no branch
    # for. Asserted here rather than in the M25-10 suite because the parity is what this item owns.
    assert CompositionContractError("not_a_blocker_code", "x").code == "invalid_contract"
    for code in BLOCKER_CODES:
        assert CompositionContractError(code, "x").code == code
