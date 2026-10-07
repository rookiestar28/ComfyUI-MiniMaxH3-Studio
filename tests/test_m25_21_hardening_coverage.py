"""M25-21 hardening coverage manifest and report join.

The manifest is generated (``scripts/nle_hardening_manifest.py``); these tests bind it to its
schema and its two frozen sources, prove the semantic validator rejects every way a coverage set
can quietly shrink or inflate, and prove the report join cannot turn a missing, misattributed or
failing observation into a pass. The frontend collection test
(``frontend/tests/nleEvidenceCollection.test.ts``) resolves every evidence ID against the runners.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from comfyui_h3_context.core.composition_contract import NLE_OPERATION_IDS
from comfyui_h3_context.core.semantic_conformance import SIDEBAR_UI_INVARIANT_IDS
from scripts import nle_hardening_manifest as generator
from scripts import nle_hardening_report as report_module

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "nle_hardening_coverage_manifest_v1.schema.json"
FRONTEND_TESTS = ROOT / "frontend" / "tests"


def _load(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, member in items:
            if key in value:
                raise AssertionError(f"duplicate JSON member: {key}")
            value[key] = member
        return value

    return cast(
        dict[str, Any], json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    )


def _manifest() -> dict[str, Any]:
    return _load(generator.MANIFEST_PATH)


def _schema_errors(manifest: dict[str, Any]) -> list[str]:
    schema = _load(SCHEMA_PATH)
    return [error.message for error in Draft202012Validator(schema).iter_errors(manifest)]


def test_manifest_is_current_and_validates_against_its_schema() -> None:
    Draft202012Validator.check_schema(_load(SCHEMA_PATH))
    assert generator.MANIFEST_PATH.read_text(encoding="utf-8") == generator.render()
    assert _schema_errors(_manifest()) == []
    generator.validate(_manifest())


def test_command_rows_are_exactly_the_33_backend_commands_joined_on_command() -> None:
    manifest = _manifest()
    control = _load(generator.CONTROL_MANIFEST_PATH)
    assert [row["command"] for row in manifest["command_rows"]] == list(NLE_OPERATION_IDS)
    by_command = {row["command"]: row["operation_id"] for row in control["command_rows"]}
    assert {row["command"]: row["operation_id"] for row in manifest["command_rows"]} == by_command
    assert manifest["joined_sources"]["control_coverage_manifest_sha256"] == generator.sha256_of(
        generator.CONTROL_MANIFEST_PATH
    )
    assert manifest["joined_sources"]["sidebar_editor_ui_contract_sha256"] == generator.sha256_of(
        generator.SHELL_CONTRACT_PATH
    )


def test_ui_rows_are_the_seven_frozen_invariants_and_never_commands() -> None:
    manifest = _manifest()
    assert tuple(row["invariant_id"] for row in manifest["ui_rows"]) == SIDEBAR_UI_INVARIANT_IDS
    assert not {row["invariant_id"] for row in manifest["ui_rows"]} & set(NLE_OPERATION_IDS)
    for row in manifest["ui_rows"]:
        assert row["expected_effects"] == generator.ZERO_EFFECTS


def test_every_row_names_three_evidence_ids_in_existing_files() -> None:
    manifest = _manifest()
    references: list[str] = []
    for section in ("command_rows", "ui_rows", "action_rows"):
        for row in manifest[section]:
            references.extend(row["evidence"].values())
            references.extend(subcase["evidence_id"] for subcase in row.get("subcases", []))
    references.extend(row["evidence_id"] for row in manifest["recovery_rows"])
    references.extend(row["evidence_id"] for row in manifest["measurements"])
    for reference in references:
        file, _, title = reference.partition("::")
        assert title.strip(), reference
        assert (FRONTEND_TESTS / file).is_file(), reference


@pytest.mark.parametrize(
    ("standalone", "workspace", "expected"),
    [(0, 1, "PASS"), (1, 1, "FAIL"), (0, 2, "FAIL"), (None, 1, "FAIL")],
)
def test_audio_allocation_quantities_are_distinct_and_missing_is_not_zero(
    standalone: int | None, workspace: int, expected: str
) -> None:
    manifest = _manifest()
    document = _passing_documents(manifest)[0]
    names = {
        "standalone": "nle_hardening.measurement.audio.standalone_allocations",
        "workspace": "nle_hardening.measurement.audio.workspace_context_allocations",
    }
    for row in list(document["observations"]):
        if row["name"] == names["standalone"]:
            if standalone is None:
                document["observations"].remove(row)
            else:
                row["body"]["observed"] = standalone
        elif row["name"] == names["workspace"]:
            row["body"]["observed"] = workspace
    result = _join([document])
    audio_rows = [
        row for row in result["measurements"] if row["measurement_id"].startswith("audio.")
    ]
    assert {row["measurement_id"] for row in audio_rows} == {
        "audio.standalone_allocations",
        "audio.workspace_context_allocations",
    }
    assert ("FAIL" if any(row["status"] == "FAIL" for row in audio_rows) else "PASS") == expected
    assert result["disposition"]["verdict"] == expected


Mutation = Callable[[dict[str, Any]], None]


def _drop_command(manifest: dict[str, Any]) -> None:
    manifest["command_rows"].pop()


def _duplicate_command(manifest: dict[str, Any]) -> None:
    manifest["command_rows"][1] = copy.deepcopy(manifest["command_rows"][0])


def _extra_command(manifest: dict[str, Any]) -> None:
    extra = copy.deepcopy(manifest["command_rows"][0])
    extra["command"] = "set_audio_gain"
    manifest["command_rows"][0] = extra


def _category_only_accessibility(manifest: dict[str, Any]) -> None:
    for row in manifest["command_rows"]:
        row["evidence"]["accessibility_evidence_id"] = (
            f"{generator.COMMANDS_SPEC}::hardening a11y every command is accessible"
        )


def _shared_recovery(manifest: dict[str, Any]) -> None:
    manifest["command_rows"][1]["evidence"]["recovery_evidence_id"] = manifest["command_rows"][0][
        "evidence"
    ]["recovery_evidence_id"]


def _stress_not_the_workload(manifest: dict[str, Any]) -> None:
    manifest["command_rows"][0]["evidence"]["stress_evidence_id"] = (
        f"{generator.STRESS_SPEC}::a smaller stress run"
    )


def _missing_trim_subcase(manifest: dict[str, Any]) -> None:
    row = next(row for row in manifest["command_rows"] if row["command"] == "trim_clip")
    row["subcases"].pop()


def _drop_ui_row(manifest: dict[str, Any]) -> None:
    manifest["ui_rows"].pop()


def _ui_row_as_command(manifest: dict[str, Any]) -> None:
    manifest["ui_rows"][0]["invariant_id"] = manifest["command_rows"][0]["command"]


def _drop_close_reason(manifest: dict[str, Any]) -> None:
    row = next(
        row for row in manifest["ui_rows"] if row["invariant_id"] == "overlay_close_return_focus"
    )
    row["close_reasons"].pop()


def _action_as_command(manifest: dict[str, Any]) -> None:
    manifest["action_rows"][0]["row_id"] = "action.clip.enabled"


def _drop_seam(manifest: dict[str, Any]) -> None:
    manifest["recovery_rows"].pop(0)


def _lower_seam_floor(manifest: dict[str, Any]) -> None:
    manifest["recovery_rows"][0]["injections_min"] = 1


def _widen_threshold(manifest: dict[str, Any]) -> None:
    row = next(
        row for row in manifest["measurements"] if row["measurement_id"] == "stress.render_p95"
    )
    row["threshold"]["value"] = 32


def _drop_measurement(manifest: dict[str, Any]) -> None:
    manifest["measurements"].pop()


def _renamed_identity(manifest: dict[str, Any]) -> None:
    manifest["command_rows"][0]["accessible_name"] = "Add"


@pytest.mark.parametrize(
    "mutation",
    [
        _drop_command,
        _duplicate_command,
        _extra_command,
        _category_only_accessibility,
        _shared_recovery,
        _stress_not_the_workload,
        _missing_trim_subcase,
        _drop_ui_row,
        _ui_row_as_command,
        _drop_close_reason,
        _action_as_command,
        _drop_seam,
        _lower_seam_floor,
        _widen_threshold,
        _drop_measurement,
        _renamed_identity,
    ],
)
def test_validator_rejects_every_shrinking_or_inflating_mutation(mutation: Mutation) -> None:
    manifest = _manifest()
    mutation(manifest)
    with pytest.raises(generator.ManifestError):
        generator.validate(manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest["command_rows"][0].__setitem__("supported", False),
        lambda manifest: manifest["command_rows"][0]["evidence"].pop("stress_evidence_id"),
        lambda manifest: manifest["command_rows"][0]["evidence"].__setitem__(
            "recovery_evidence_id", ""
        ),
        lambda manifest: manifest["ui_rows"][0]["expected_effects"].__setitem__("queue", 1),
        lambda manifest: manifest["command_rows"][0]["evidence"].__setitem__(
            "accessibility_evidence_id", "skip"
        ),
    ],
)
def test_schema_rejects_unsupported_skips_and_nonzero_effects(mutation: Mutation) -> None:
    manifest = _manifest()
    mutation(manifest)
    assert _schema_errors(manifest)


# ----------------------------------------------------------------------------- report join


def _passing_documents(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    tests: dict[str, str] = {}
    observations: list[dict[str, Any]] = []

    def record(name: str, test_id: str, body: dict[str, Any]) -> None:
        tests[test_id] = "passed"
        observations.append({"name": f"nle_hardening.{name}", "test_id": test_id, "body": body})

    verdict = {"status": "PASS", "assertions": ["synthetic"], "facts": {}}
    for row in manifest["command_rows"]:
        for dimension in generator.DIMENSIONS:
            record(
                f"command.{row['command']}.{dimension}",
                row["evidence"][f"{dimension}_evidence_id"],
                verdict,
            )
        for subcase in row.get("subcases", []):
            record(
                f"command.{row['command']}.{subcase['subcase_id']}",
                subcase["evidence_id"],
                verdict,
            )
    for row in manifest["ui_rows"]:
        for dimension in generator.DIMENSIONS:
            record(
                f"ui.{row['invariant_id']}.{dimension}",
                row["evidence"][f"{dimension}_evidence_id"],
                verdict,
            )
    for row in manifest["action_rows"]:
        for dimension in generator.DIMENSIONS:
            record(
                f"{row['row_id']}.{dimension}",
                row["evidence"][f"{dimension}_evidence_id"],
                verdict,
            )
    for row in manifest["recovery_rows"]:
        record(
            f"seam.{row['seam_id'].removeprefix('seam.')}.recovery",
            row["evidence_id"],
            {**verdict, "facts": {"injections": row["injections_min"]}},
        )
    for row in manifest["measurements"]:
        threshold = row["threshold"]
        record(
            f"measurement.{row['measurement_id']}",
            row["evidence_id"],
            {
                "observed": threshold["value"],
                "unit": row["unit"],
                "method": "synthetic",
                "sample_count": 1,
                "interval_ms": 1,
            },
        )
    workloads = {
        "smoke": generator.SMOKE_WORKLOAD,
        "stress": generator.PLAYBACK_WORKLOAD,
        "render": generator.OUTPUT_WORKLOAD,
        "ui_stress": generator.UI_WORKLOAD,
    }
    for key, test_id in workloads.items():
        record(f"workload.{key}.identity", test_id, verdict)
    identity = {field: "synthetic" for field in report_module.IDENTITY_FIELDS}
    identity["manifest_sha256"] = SHA
    return [
        {
            "schema": report_module.OBSERVATIONS_SCHEMA,
            "identity": identity,
            "tests": [{"test_id": test_id, "status": status} for test_id, status in tests.items()],
            "observations": observations,
        }
    ]


PASSING_AUDIT: dict[str, Any] = {
    "schema": report_module.AUDIT_SCHEMA,
    "status": "PASS",
    "entries": [
        {"name": "react", "version": "19.2.8", "license": "MIT", "disposition": "unchanged"}
    ],
}
SHA = "sha256:" + "0" * 64


def _join(documents: list[dict[str, Any]], audit: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report_module.join(
        _manifest(),
        documents,
        PASSING_AUDIT if audit is None else audit,
        manifest_sha256=SHA,
    )
    report_module.validate_report(report)
    return report


def _observation(documents: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(
        observation
        for observation in documents[0]["observations"]
        if observation["name"] == f"nle_hardening.{name}"
    )


def test_a_complete_passing_observation_set_joins_to_pass_with_the_frozen_sections() -> None:
    report = _join(_passing_documents(_manifest()))
    assert tuple(report) == ("schema", *report_module.REPORT_SECTIONS)
    assert report["disposition"]["verdict"] == "PASS"
    # 33 accepted commands and `set_clip_audio`.
    assert len(report["command_rows"]) == 34
    assert len(report["ui_rows"]) == 7


def test_a_missing_observation_is_not_run_and_the_report_is_incomplete() -> None:
    documents = _passing_documents(_manifest())
    documents[0]["observations"] = [
        observation
        for observation in documents[0]["observations"]
        if observation["name"] != "nle_hardening.command.trim_clip.recovery"
    ]
    trim_title = next(
        row["evidence"]["recovery_evidence_id"]
        for row in _manifest()["command_rows"]
        if row["command"] == "trim_clip"
    )
    documents[0]["tests"] = [
        test for test in documents[0]["tests"] if test["test_id"] != trim_title
    ]
    report = _join(documents)
    row = next(row for row in report["command_rows"] if row["command"] == "trim_clip")
    assert row["dimensions"]["recovery"]["status"] == "NOT_RUN"
    assert row["status"] == "NOT_RUN"
    assert report["disposition"]["verdict"] == "INCOMPLETE"


def test_an_observation_from_another_test_is_a_failure_not_a_pass() -> None:
    documents = _passing_documents(_manifest())
    observation = _observation(documents, "command.set_clip_enabled.accessibility")
    observation["test_id"] = generator.EDIT_WORKLOAD
    report = _join(documents)
    row = next(row for row in report["command_rows"] if row["command"] == "set_clip_enabled")
    assert row["dimensions"]["accessibility"]["status"] == "FAIL"
    assert report["disposition"]["verdict"] == "FAIL"


def test_an_observation_from_a_failing_test_cannot_pass() -> None:
    documents = _passing_documents(_manifest())
    title = _observation(documents, "ui.function_switch.recovery")["test_id"]
    for test in documents[0]["tests"]:
        if test["test_id"] == title:
            test["status"] = "failed"
    report = _join(documents)
    row = next(row for row in report["ui_rows"] if row["invariant_id"] == "function_switch")
    assert row["dimensions"]["recovery"]["status"] == "FAIL"
    assert report["disposition"]["verdict"] == "FAIL"


def test_an_observation_without_identified_assertions_cannot_pass() -> None:
    documents = _passing_documents(_manifest())
    _observation(documents, "command.undo.stress")["body"]["assertions"] = []
    report = _join(documents)
    assert report["disposition"]["verdict"] == "FAIL"


def test_a_measurement_beyond_its_frozen_threshold_fails() -> None:
    documents = _passing_documents(_manifest())
    _observation(documents, "measurement.stress.render_p95")["body"]["observed"] = 16.5
    report = _join(documents)
    row = next(
        row for row in report["measurements"] if row["measurement_id"] == "stress.render_p95"
    )
    assert row["status"] == "FAIL"
    assert report["disposition"]["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "measurement_id",
    [
        "floor.move_drags",
        "floor.group_move_drags",
        "floor.cross_track_moves",
        "floor.marquee_selections",
        "floor.ruler_scrubs",
    ],
)
def test_removing_each_direct_manipulation_action_floor_fails(measurement_id: str) -> None:
    documents = _passing_documents(_manifest())
    _observation(documents, f"measurement.{measurement_id}")["body"]["observed"] = 0
    report = _join(documents)
    row = next(row for row in report["measurements"] if row["measurement_id"] == measurement_id)
    assert row["status"] == "FAIL"
    assert report["disposition"]["verdict"] == "FAIL"


def test_a_missing_measurement_is_not_run_never_zero() -> None:
    documents = _passing_documents(_manifest())
    documents[0]["observations"] = [
        observation
        for observation in documents[0]["observations"]
        if observation["name"] != "nle_hardening.measurement.stress.heap_growth"
    ]
    title = generator.PLAYBACK_WORKLOAD
    for test in documents[0]["tests"]:
        if test["test_id"] == title:
            test["status"] = "not_run"
    report = _join(documents)
    row = next(
        row for row in report["measurements"] if row["measurement_id"] == "stress.heap_growth"
    )
    assert row["status"] == "NOT_RUN"
    assert "observed" not in row


def test_a_measurement_without_a_method_or_samples_fails() -> None:
    documents = _passing_documents(_manifest())
    del _observation(documents, "measurement.floor.seeks")["body"]["sample_count"]
    report = _join(documents)
    assert report["disposition"]["verdict"] == "FAIL"


def test_seam_injections_below_the_floor_fail() -> None:
    documents = _passing_documents(_manifest())
    _observation(documents, "seam.compositor.recovery")["body"]["facts"]["injections"] = 9
    report = _join(documents)
    row = next(row for row in report["recovery_rows"] if row["seam_id"] == "seam.compositor")
    assert row["status"] == "FAIL"


def test_a_changed_dependency_or_missing_audit_blocks_pass() -> None:
    documents = _passing_documents(_manifest())
    changed = copy.deepcopy(PASSING_AUDIT)
    changed["entries"][0]["disposition"] = "added"
    assert _join(documents, changed)["disposition"]["verdict"] == "FAIL"
    report = report_module.join(_manifest(), documents, None, manifest_sha256=SHA)
    assert report["disposition"]["verdict"] == "INCOMPLETE"


def test_a_pinned_package_identifier_joins_while_a_real_payload_still_rejects() -> None:
    documents = _passing_documents(_manifest())
    named = copy.deepcopy(PASSING_AUDIT)
    named["entries"].append(
        {
            "name": "tough-cookie",
            "version": "6.0.2",
            "license": "BSD-3-Clause",
            "disposition": "unchanged",
        }
    )
    report = _join(documents, named)
    assert any(entry["name"] == "tough-cookie" for entry in report["dependency_audit"]["entries"])
    carried = copy.deepcopy(named)
    carried["entries"][-1]["license"] = "cookie=SESSION"
    with pytest.raises(report_module.ReportError):
        report_module.join(_manifest(), documents, carried, manifest_sha256=SHA)
    unpinned = copy.deepcopy(PASSING_AUDIT)
    unpinned["entries"].append(
        {
            "name": "harvest-cookie",
            "version": "1.0.0",
            "license": "MIT",
            "disposition": "unchanged",
        }
    )
    with pytest.raises(report_module.ReportError):
        report_module.join(_manifest(), documents, unpinned, manifest_sha256=SHA)


def test_privacy_unsafe_or_duplicate_observations_are_rejected_before_joining() -> None:
    documents = _passing_documents(_manifest())
    unsafe = copy.deepcopy(documents)
    _observation(unsafe, "command.undo.stress")["body"]["facts"] = {
        "path": "C:\\Users\\someone\\media.mp4"
    }
    with pytest.raises(report_module.ReportError):
        report_module.validate_observations(unsafe[0])
    duplicate = copy.deepcopy(documents)
    duplicate[0]["observations"].append(duplicate[0]["observations"][0])
    with pytest.raises(report_module.ReportError):
        report_module.validate_observations(duplicate[0])


def test_an_oversized_row_summary_fails_its_dimension() -> None:
    documents = _passing_documents(_manifest())
    _observation(documents, "command.undo.accessibility")["body"]["facts"] = {
        "padding": "x" * (report_module.MAX_ROW_SUMMARY_BYTES + 1)
    }
    report = _join(documents)
    row = next(row for row in report["command_rows"] if row["command"] == "undo")
    assert row["dimensions"]["accessibility"]["status"] == "FAIL"


def test_a_report_with_a_tampered_disposition_or_unlisted_na_is_rejected() -> None:
    report = _join(_passing_documents(_manifest()))
    tampered = copy.deepcopy(report)
    tampered["command_rows"][0]["status"] = "FAIL"
    with pytest.raises(report_module.ReportError):
        report_module.validate_report(tampered)
    not_applicable = copy.deepcopy(report)
    not_applicable["measurements"][0]["status"] = "N/A"
    not_applicable["disposition"] = report_module.disposition(not_applicable)
    with pytest.raises(report_module.ReportError):
        report_module.validate_report(not_applicable)
    reordered = {key: report[key] for key in reversed(list(report))}
    with pytest.raises(report_module.ReportError):
        report_module.validate_report(reordered)


def test_identity_gathered_against_another_manifest_fails() -> None:
    documents = _passing_documents(_manifest())
    documents[0]["identity"]["manifest_sha256"] = "sha256:" + "1" * 64
    report = report_module.join(_manifest(), documents, PASSING_AUDIT, manifest_sha256=SHA)
    assert report["identity"]["status"] == "FAIL"
    assert report["disposition"]["verdict"] == "FAIL"
