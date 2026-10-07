"""M25-16 control-coverage manifest: one executable control per M25-11 command.

The manifest is authored data under ``governance/contracts``; this reader binds it to the
closed backend operation profile so a supported command with no executable control row, or a
control row naming a command outside the profile, fails here rather than in review. The
frontend reader (``frontend/tests/nleWorkspaceManifest.test.tsx``) binds the same rows to the
rendered controls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from comfyui_h3_context.core.composition_contract import NLE_OPERATION_IDS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "governance" / "contracts" / "nle_control_coverage_manifest_v1.json"
SCHEMA_PATH = ROOT / "governance" / "contracts" / "nle_control_coverage_manifest_v1.schema.json"
AUTHORITY_CLASSES = (
    "canonical_edit",
    "canonical_derived_effect",
    "local_transport",
    "output_action",
    "read_only_observation",
    "deferred_unavailable",
)


def _load(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in items:
            if key in value:
                raise AssertionError(f"duplicate JSON member: {key}")
            value[key] = member
        return value

    return cast(
        dict[str, Any],
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs),
    )


def test_manifest_validates_against_its_schema() -> None:
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(_load(MANIFEST_PATH)),
        key=lambda error: list(error.path),
    )
    assert errors == [], [error.message for error in errors]


def test_every_profile_command_has_exactly_one_canonical_control_row() -> None:
    manifest = _load(MANIFEST_PATH)
    commands = [row["command"] for row in manifest["command_rows"]]
    assert sorted(commands) == sorted(NLE_OPERATION_IDS)
    assert len(set(commands)) == len(NLE_OPERATION_IDS)
    for row in manifest["command_rows"]:
        assert row["authority_class"] == "canonical_edit"
        assert row["command"] in NLE_OPERATION_IDS
        assert row["control_selector"].startswith('[data-h3-nle-control="')
        assert row["accepted_projection_selector"]
        assert row["route"].endswith("apply_timeline_transaction")


def test_schema_v2_gestures_and_alternates_never_blur_row_identity() -> None:
    # M25-44: a row names its primary gesture and any alternates; an alternate is never another
    # row's canonical selector (two canonical rows would then share one control).
    manifest = _load(MANIFEST_PATH)
    assert manifest["schema"] == "h3.context.nle_control_coverage_manifest.v2"
    rows = manifest["command_rows"] + manifest["non_command_rows"]
    canonical = {row["control_selector"] for row in rows if row["control_selector"] is not None}
    for row in rows:
        if row["control_selector"] is None:
            assert row["gesture"] is None and row["alternate_selectors"] == [], row
        if row in manifest["command_rows"]:
            assert row["gesture"] is not None, row["operation_id"]
        alternates = row["alternate_selectors"]
        assert len(alternates) == len(set(alternates)), row["operation_id"]
        assert row["control_selector"] not in alternates, row["operation_id"]
        assert not canonical.intersection(alternates), row["operation_id"]


def test_non_command_rows_never_masquerade_as_canonical_edits() -> None:
    manifest = _load(MANIFEST_PATH)
    assert list(manifest["authority_classes"]) == list(AUTHORITY_CLASSES)
    ids = [row["operation_id"] for row in manifest["command_rows"] + manifest["non_command_rows"]]
    assert len(ids) == len(set(ids)), "operation identifiers must be unique across both tables"
    for row in manifest["non_command_rows"]:
        assert row["authority_class"] != "canonical_edit"
        # A non-command row may name a Production action, never an M25-11 timeline command.
        assert row["command"] not in NLE_OPERATION_IDS
        if row["authority_class"] == "local_transport":
            assert row["command"] is None and row["route"] is None
        if row["authority_class"] == "deferred_unavailable":
            assert row["control_selector"] is None
            assert row["command"] is None and row["route"] is None
    by_id = {row["operation_id"]: row for row in manifest["non_command_rows"]}
    assert by_id["asset.import_production"]["route"] == "/h3-context/v1/production/authoring-import"
    assert by_id["audio.embedded_primary_follow"]["authority_class"] == "canonical_derived_effect"
    assert by_id["output.render"]["authority_class"] == "output_action"


# Runner collection belongs to the frontend suite, where the pinned Node dependencies exist.
# nleEvidenceCollection.test.ts invokes both runners rather than parsing JavaScript strings:
# a template declaration or an operation mentioned elsewhere is not an expanded collected case.
FRONTEND_TESTS = ROOT / "frontend" / "tests"
EVIDENCE_FACETS = ("pointer", "keyboard", "stress", "recovery", "effect")


def test_evidence_ids_name_bounded_test_files_and_concrete_cases() -> None:
    manifest = _load(MANIFEST_PATH)
    for row in manifest["command_rows"] + manifest["non_command_rows"]:
        assert set(row["evidence"]) == set(EVIDENCE_FACETS)
        for facet in EVIDENCE_FACETS:
            reference = row["evidence"][facet]
            relative, separator, title = reference.partition("::")
            assert separator == "::" and title.strip(), reference
            assert "${" not in title, f"unexpanded evidence title: {reference}"
            path = (FRONTEND_TESTS / relative).resolve()
            assert path.is_relative_to(FRONTEND_TESTS.resolve()), reference
            assert path.is_file(), f"evidence file does not exist: {relative}"
            assert relative.endswith((".test.ts", ".test.tsx", ".spec.ts")), reference


def test_command_rows_cite_distinct_accepted_and_refused_paths() -> None:
    # Accepted keyboard activation and refusal/recovery must cite distinct executable cases.
    for row in _load(MANIFEST_PATH)["command_rows"]:
        evidence = row["evidence"]
        assert evidence["keyboard"] != evidence["recovery"], row["operation_id"]
        assert evidence["keyboard"] != evidence["effect"], row["operation_id"]
        assert "nleCommandMatrix.spec.ts" in evidence["keyboard"], row["operation_id"]
        assert "stale revision is refused" in evidence["recovery"] or (
            row["operation_id"] == "clip.trim" and "lost response" in evidence["recovery"]
        ), row["operation_id"]
