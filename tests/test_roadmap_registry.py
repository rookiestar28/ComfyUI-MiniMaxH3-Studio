"""bounded active-roadmap index and archive regressions."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from scripts import roadmap_registry as registry
from scripts.roadmap_registry import (
    ACTIVE_INDEX_SCHEMA,
    DEFAULT_ARCHIVE_PATH,
    DEFAULT_INDEX_PATH,
    MAX_INDEX_BYTES,
    RoadmapIndexError,
    _coordination_plan_paths,
    _dependency_waves,
    _track,
    archive_authorities,
    history_lookup,
    load_index,
    main,
    render_roadmap,
    restore_check,
)

ROOT = Path(__file__).resolve().parents[1]


def test_active_index_schema_is_v3() -> None:
    assert ACTIVE_INDEX_SCHEMA == "h3-context-active-roadmap-index/3"


def test_index_budget_and_large_archive_projection_are_in_lockstep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # CRITICAL: a small registry cannot detect the old independent projection read ceiling.
    # Fill the actual index budget so restoring only that old literal fails this readback.
    assert MAX_INDEX_BYTES == 512_000
    root, index = _repo(tmp_path)
    archives = _dict_list(index["archives"])
    source = Path(".tmp/roadmap.json")
    for number in range(400):
        archive = Path(".planning/roadmap/archive") / f"synthetic-{number:04d}"
        manifest = archive_authorities(root, [source], archive)
        row = {
            "path": archive.as_posix(),
            "manifest_sha256": manifest["manifest_sha256"],
            "item_count": 1,
            "description": "Synthetic immutable archive " + "x" * 1870,
        }
        archives.append(row)
        # The fixture writer uses native text newlines; count the actual Windows bytes too.
        raw = (
            (json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            .replace("\n", os.linesep)
            .encode("utf-8")
        )
        if len(raw) > MAX_INDEX_BYTES:
            archives.pop()
            break
    else:
        pytest.fail("capacity fixture did not approach the actual index limit")
    _write_json(root / DEFAULT_INDEX_PATH, index)
    actual_bytes = len((root / DEFAULT_INDEX_PATH).read_bytes())
    assert 0 < MAX_INDEX_BYTES - actual_bytes < 2500

    generated = render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
    archive_view = root / ".planning/roadmap/views/ARCHIVES.md"
    assert 128_000 < archive_view.stat().st_size <= MAX_INDEX_BYTES
    checked = render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)
    dashboard = (root / "ROADMAP.md").read_bytes()
    assert len(dashboard) <= registry.MAX_ROOT_BYTES
    assert max(map(len, dashboard.decode("utf-8").splitlines())) <= registry.MAX_ROOT_LINE
    assert str(archives[-1]["description"]) in archive_view.read_text(encoding="utf-8")

    for result in (generated, checked):
        assert result["index_bytes"] == actual_bytes
        assert result["index_headroom"] == MAX_INDEX_BYTES - actual_bytes
    assert "headroom" in capsys.readouterr().err.lower()


def test_check_command_reports_budget_without_replacing_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _index_value = _repo(tmp_path)
    path = root / DEFAULT_INDEX_PATH
    before = path.read_bytes()

    assert main(["check", "--index", DEFAULT_INDEX_PATH.as_posix()], root=root) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PASS"
    assert result["index_bytes"] == len(before)
    assert result["index_headroom"] == MAX_INDEX_BYTES - len(before)
    assert path.read_bytes() == before


def _release_repo(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root, index = _repo(tmp_path)
    index["schema"] = "h3-context-active-roadmap-index/3"
    rows = _dict_list(index["items"])
    for row in rows:
        row["lane"] = "standard"
    release = rows[-1]
    release["id"] = "M90-99"
    release["plan_path"] = _plan(root, "M90-99")
    release["plan_state"] = "FINALIZED"
    release["release"] = {
        "appetite": 2,
        "admission": ["owner_instruction", "security_privacy"],
        "frozen_at": "2026-01-01T12:00:00+08:00",
    }
    index["release_scope_changes"] = [
        {
            "date": "2026-01-01T12:00:00+08:00",
            "release_id": "M90-99",
            "change": "added",
            "item": "RM-18",
            "criterion": "owner_instruction",
            "instruction": "The fixture owner approved this dependency.",
            "note": "Synthetic initial scope.",
        }
    ]
    _write_json(root / DEFAULT_INDEX_PATH, index)
    return root, index


def test_closed_release_scope_replays_archived_and_active_members(tmp_path: Path) -> None:
    root, index = _release_repo(tmp_path)
    release = _dict_list(index["items"])[-1]
    _string_list(release["depends_on"]).append("M16-03")
    event = deepcopy(_dict_list(index["release_scope_changes"])[0])
    event["item"] = "M16-03"
    _dict_list(index["release_scope_changes"]).append(event)
    _write_json(root / DEFAULT_INDEX_PATH, index)

    assert load_index(root, DEFAULT_INDEX_PATH) == index


def test_owner_scope_amendment_preserves_dependencies_and_appetite(tmp_path: Path) -> None:
    root, index = _release_repo(tmp_path)
    release = _dict_list(index["items"])[-1]
    dependencies = deepcopy(release["depends_on"])
    cast(dict[str, object], release["release"])["appetite"] = len(cast(list[object], dependencies))
    event = deepcopy(_dict_list(index["release_scope_changes"])[0])
    event.update(
        change="amended",
        date="2026-01-01T12:00:01+08:00",
        instruction="The owner amended this release's use of the admitted gate.",
    )
    _dict_list(index["release_scope_changes"]).append(event)
    _write_json(root / DEFAULT_INDEX_PATH, index)
    before = (root / DEFAULT_INDEX_PATH).read_bytes()
    assert load_index(root, DEFAULT_INDEX_PATH) == index
    assert release["depends_on"] == dependencies
    assert (root / DEFAULT_INDEX_PATH).read_bytes() == before


def test_scope_amendment_cannot_admit_an_absent_item(tmp_path: Path) -> None:
    root, index = _release_repo(tmp_path)
    event = deepcopy(_dict_list(index["release_scope_changes"])[0])
    event.update(change="amended", item="M16-03", date="2026-01-01T12:00:01+08:00")
    _dict_list(index["release_scope_changes"]).append(event)
    _write_json(root / DEFAULT_INDEX_PATH, index)
    before = (root / DEFAULT_INDEX_PATH).read_bytes()
    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)
    assert (root / DEFAULT_INDEX_PATH).read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_bootstrap",
        "unknown_criterion",
        "unadmitted_criterion",
        "empty_instruction",
        "naive_date",
        "pre_freeze_date",
        "duplicate_addition",
        "absent_removal",
        "unknown_item",
        "unknown_release",
        "boolean_appetite",
        "zero_appetite",
        "oversized_appetite",
        "unrecorded_dependency",
        "appetite_overflow",
        "release_unknown_member",
        "row_unknown_member",
    ],
)
def test_release_scope_refuses_forged_or_incomplete_events_without_writes(
    tmp_path: Path, mutation: str
) -> None:
    root, index = _release_repo(tmp_path)
    release = _dict_list(index["items"])[-1]
    block = cast(dict[str, object], release["release"])
    ledger = _dict_list(index["release_scope_changes"])
    event = ledger[0]
    if mutation == "missing_bootstrap":
        ledger.clear()
    elif mutation == "unknown_criterion":
        event["criterion"] = "invented"
    elif mutation == "unadmitted_criterion":
        event["criterion"] = "data_loss"
    elif mutation == "empty_instruction":
        event["instruction"] = " "
    elif mutation == "naive_date":
        event["date"] = "2026-01-01T12:00:00"
    elif mutation == "pre_freeze_date":
        event["date"] = "2026-01-01T11:59:59+08:00"
    elif mutation == "duplicate_addition":
        extra = deepcopy(event)
        extra["date"] = "2026-01-01T12:00:01+08:00"
        ledger.append(extra)
    elif mutation == "absent_removal":
        event["change"] = "removed"
    elif mutation == "unknown_item":
        event["item"] = "M90-404"
    elif mutation == "unknown_release":
        event["release_id"] = "M90-404"
    elif mutation == "boolean_appetite":
        block["appetite"] = True
    elif mutation == "zero_appetite":
        block["appetite"] = 0
    elif mutation == "oversized_appetite":
        block["appetite"] = 257
    elif mutation == "unrecorded_dependency":
        _string_list(release["depends_on"]).append("M16-03")
    elif mutation == "appetite_overflow":
        block["appetite"] = 1
        _string_list(release["depends_on"]).append("M16-03")
        extra = deepcopy(event)
        extra["item"] = "M16-03"
        ledger.append(extra)
    elif mutation == "release_unknown_member":
        block["implicit_scope"] = []
    elif mutation == "row_unknown_member":
        event["approved"] = True
    else:
        raise AssertionError(mutation)
    path = root / DEFAULT_INDEX_PATH
    _write_json(path, index)
    before = path.read_bytes()

    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)

    assert path.read_bytes() == before
    assert not (root / "ROADMAP.md").exists()


def test_release_scope_rejects_temporary_overflow_even_when_final_set_matches(
    tmp_path: Path,
) -> None:
    root, index = _release_repo(tmp_path)
    release = _dict_list(index["items"])[-1]
    cast(dict[str, object], release["release"])["appetite"] = 1
    ledger = _dict_list(index["release_scope_changes"])
    addition = deepcopy(ledger[0])
    addition.update(item="M16-03", date="2026-01-01T12:00:01+08:00")
    removal = deepcopy(addition)
    removal.update(change="removed", date="2026-01-01T12:00:02+08:00")
    ledger.extend([addition, removal])
    _write_json(root / DEFAULT_INDEX_PATH, index)

    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)


def test_release_waves_follow_the_declared_release_instead_of_a_legacy_id(
    tmp_path: Path,
) -> None:
    root, _index_value = _release_repo(tmp_path)

    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)

    projection = (root / "ROADMAP.md").read_text(encoding="utf-8")
    assert "M90-99" in projection
    assert "| Wave 0 | `RM-17`" in projection
    assert "| Wave 1 | `RM-18`" in projection
    assert "| Wave 2 | `M90-99`" in projection


def test_registry_with_no_active_release_has_no_ledger_or_wave_projection(tmp_path: Path) -> None:
    root, index = _release_repo(tmp_path)
    del _dict_list(index["items"])[-1]["release"]
    index["release_scope_changes"] = []
    _write_json(root / DEFAULT_INDEX_PATH, index)

    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)

    assert "| Wave 0 |" not in (root / "ROADMAP.md").read_text(encoding="utf-8")
    _dict_list(index["release_scope_changes"]).append({"release_id": "M90-99"})
    _write_json(root / DEFAULT_INDEX_PATH, index)
    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)


def _legacy_migration_repo(tmp_path: Path) -> tuple[Path, dict[str, object], bytes]:
    root, index = _repo(tmp_path)
    index["schema"] = "h3-context-active-roadmap-index/2"
    index.pop("release_scope_changes", None)
    for row in _dict_list(index["items"]):
        row.pop("lane", None)
        row.pop("release", None)
    _dict_list(index["items"])[-1]["id"] = "M90-99"
    _write_json(root / DEFAULT_INDEX_PATH, index)
    return root, index, (root / DEFAULT_INDEX_PATH).read_bytes()


def _migration_scope(root: Path) -> str:
    relative = ".planning/synthetic-scope.json"
    _write_json(
        root / relative,
        {
            "release_id": "M90-99",
            "release": {
                "appetite": 2,
                "admission": ["owner_instruction"],
                "frozen_at": "2026-01-01T12:00:00+08:00",
            },
            "instruction": "The synthetic owner approved the existing dependency scope.",
            "note": "Inert fixture scope, unrelated to an actual release.",
        },
    )
    return relative


def _migrate_arguments(before: bytes) -> list[str]:
    return [
        "migrate",
        "--index",
        DEFAULT_INDEX_PATH.as_posix(),
        "--expected-sha256",
        hashlib.sha256(before).hexdigest(),
        "--backup",
        ".planning/synthetic-before.json",
        "--output",
        "ROADMAP.md",
    ]


def test_explicit_migration_preserves_original_values_and_exact_backup(
    tmp_path: Path,
) -> None:
    root, previous, before = _legacy_migration_repo(tmp_path)
    scope = _migration_scope(root)

    assert main([*_migrate_arguments(before), "--scope", scope], root=root) == 0

    migrated = load_index(root, DEFAULT_INDEX_PATH)
    assert migrated["schema"] == "h3-context-active-roadmap-index/3"
    assert (root / ".planning/synthetic-before.json").read_bytes() == before
    restored = deepcopy(migrated)
    restored["schema"] = "h3-context-active-roadmap-index/2"
    restored.pop("release_scope_changes")
    for row in _dict_list(restored["items"]):
        row.pop("lane")
        row.pop("release", None)
    assert restored == previous
    ledger = _dict_list(migrated["release_scope_changes"])
    assert [row["item"] for row in ledger] == _dict_list(previous["items"])[-1]["depends_on"]
    assert all(row["criterion"] == "owner_instruction" for row in ledger)
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)


def test_no_release_migration_preserves_marked_heavy_and_rejects_implicit_legacy_load(
    tmp_path: Path,
) -> None:
    root, previous, _before = _legacy_migration_repo(tmp_path)
    _dict_list(previous["items"])[0]["title"] = "[IMPORTANT] Synthetic storage migration"
    _write_json(root / DEFAULT_INDEX_PATH, previous)
    before = (root / DEFAULT_INDEX_PATH).read_bytes()
    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)

    assert main([*_migrate_arguments(before), "--no-release"], root=root) == 0

    migrated = load_index(root, DEFAULT_INDEX_PATH)
    rows = _dict_list(migrated["items"])
    assert rows[0]["lane"] == "heavy"
    assert all(row["lane"] == "standard" for row in rows[1:])
    assert migrated["release_scope_changes"] == []
    assert "| Wave 0 |" not in (root / "ROADMAP.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("problem", ["stale_digest", "invalid_scope", "existing_backup"])
def test_migration_refuses_without_replacing_source_or_projections(
    tmp_path: Path, problem: str
) -> None:
    root, _previous, before = _legacy_migration_repo(tmp_path)
    scope = _migration_scope(root)
    arguments = [*_migrate_arguments(before), "--scope", scope]
    backup = root / ".planning/synthetic-before.json"
    if problem == "stale_digest":
        arguments[arguments.index("--expected-sha256") + 1] = "0" * 64
    elif problem == "invalid_scope":
        value = json.loads((root / scope).read_bytes())
        value["approved"] = True
        _write_json(root / scope, value)
    else:
        backup.write_bytes(b"retain existing recovery bytes")
    backup_before = backup.read_bytes() if backup.exists() else None

    with pytest.raises(RoadmapIndexError):
        main(arguments, root=root)

    assert (root / DEFAULT_INDEX_PATH).read_bytes() == before
    assert not (root / "ROADMAP.md").exists()
    assert not (root / ".planning/roadmap/views").exists()
    assert (backup.read_bytes() if backup.exists() else None) == backup_before


def test_migration_rechecks_source_after_proposal_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, previous, before = _legacy_migration_repo(tmp_path)
    scope = _migration_scope(root)
    original_render = registry._render_outputs
    concurrent = deepcopy(previous)
    _dict_list(concurrent["items"])[0]["summary"] = "Newer owner's metadata must survive"
    replacement = (json.dumps(concurrent, ensure_ascii=False, indent=2) + "\n").encode()

    def render_after_concurrent_write(index: dict[str, object]) -> dict[Path, bytes]:
        (root / DEFAULT_INDEX_PATH).write_bytes(replacement)
        return original_render(index)

    monkeypatch.setattr(registry, "_render_outputs", render_after_concurrent_write)
    with pytest.raises(RoadmapIndexError):
        main([*_migrate_arguments(before), "--scope", scope], root=root)

    assert (root / DEFAULT_INDEX_PATH).read_bytes() == replacement
    assert not (root / "ROADMAP.md").exists()


def test_projection_failure_preserves_valid_migrated_index_and_explicit_render_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _previous, before = _legacy_migration_repo(tmp_path)
    scope = _migration_scope(root)
    original_write = registry._atomic_write

    def refuse_root_projection(workspace: Path, relative: Path, raw: bytes, field: str) -> None:
        if relative == Path("ROADMAP.md"):
            raise OSError("synthetic projection lock")
        original_write(workspace, relative, raw, field)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "_atomic_write", refuse_root_projection)
        with pytest.raises(RoadmapIndexError, match="projection|render"):
            main([*_migrate_arguments(before), "--scope", scope], root=root)

    assert load_index(root, DEFAULT_INDEX_PATH)["schema"] == "h3-context-active-roadmap-index/3"
    assert (root / ".planning/synthetic-before.json").read_bytes() == before
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)


def test_ledger_compares_instants_across_offsets_and_preserves_distinct_ties(
    tmp_path: Path,
) -> None:
    root, index = _release_repo(tmp_path)
    ledger = _dict_list(index["release_scope_changes"])
    added = deepcopy(ledger[0])
    added.update(item="M16-03", date="2026-01-01T04:00:01+00:00")
    removed = deepcopy(added)
    removed.update(change="removed", date="2026-01-01T05:00:02+01:00")
    ledger.extend([added, removed])
    _write_json(root / DEFAULT_INDEX_PATH, index)
    assert load_index(root, DEFAULT_INDEX_PATH) == index

    # Lexicographic timestamp sorting would accept this reversed actual instant.
    removed["date"] = "2026-01-01T12:00:00+08:00"
    _write_json(root / DEFAULT_INDEX_PATH, index)
    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)


def test_same_item_same_instant_refuses_even_when_add_remove_final_set_matches(
    tmp_path: Path,
) -> None:
    root, index = _release_repo(tmp_path)
    ledger = _dict_list(index["release_scope_changes"])
    added = deepcopy(ledger[0])
    added.update(item="M16-03", date="2026-01-01T12:00:01+08:00")
    removed = deepcopy(added)
    removed.update(change="removed", date="2026-01-01T04:00:01+00:00")
    ledger.extend([added, removed])
    _write_json(root / DEFAULT_INDEX_PATH, index)
    with pytest.raises(RoadmapIndexError):
        load_index(root, DEFAULT_INDEX_PATH)


def test_ambiguous_release_refuses_before_projection_replacement(tmp_path: Path) -> None:
    root, index = _release_repo(tmp_path)
    rows = _dict_list(index["items"])
    rows[0]["release"] = deepcopy(rows[-1]["release"])
    _write_json(root / DEFAULT_INDEX_PATH, index)
    before = (root / DEFAULT_INDEX_PATH).read_bytes()
    with pytest.raises(RoadmapIndexError):
        render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
    assert (root / DEFAULT_INDEX_PATH).read_bytes() == before
    assert not (root / "ROADMAP.md").exists()


def _retirement_arguments(root: Path, index: dict[str, object]) -> list[str]:
    release = _dict_list(index["items"])[-1]
    terminal = {key: release[key] for key in ("id", "title", "depends_on", "owner", "plan_path")}
    terminal.update(
        status="DONE",
        commit="c" * 40,
        tree="d" * 40,
        record_path=".planning/260101-SYNTHETIC_IMPLEMENTATION_RECORD.md",
    )
    _write_json(
        root / ".tmp/terminal_items.json",
        {"schema": "h3-context-roadmap-terminal-items/1", "items": [terminal]},
    )
    return [
        "retire-release",
        "--index",
        DEFAULT_INDEX_PATH.as_posix(),
        "--release",
        "M90-99",
        "--expected-sha256",
        hashlib.sha256((root / DEFAULT_INDEX_PATH).read_bytes()).hexdigest(),
        "--terminal",
        ".tmp/terminal_items.json",
        "--archive",
        ".planning/roadmap/archive/release-end",
        "--output",
        "ROADMAP.md",
    ]


def test_retirement_archives_scope_and_terminal_bytes_then_preserves_other_rows(
    tmp_path: Path,
) -> None:
    root, previous = _release_repo(tmp_path)
    arguments = _retirement_arguments(root, previous)
    terminal_before = (root / ".tmp/terminal_items.json").read_bytes()

    assert main(arguments, root=root) == 0

    current = load_index(root, DEFAULT_INDEX_PATH)
    assert current["items"] == _dict_list(previous["items"])[:-1]
    assert current["release_scope_changes"] == []
    assert current["latest_accepted"] == previous["latest_accepted"]
    assert _dict_list(current["archives"])[:-1] == previous["archives"]
    archive = Path(".planning/roadmap/archive/release-end")
    manifest = json.loads((root / archive / "manifest.json").read_bytes())
    snapshots = [
        root / archive / entry["path"]
        for entry in manifest["entries"]
        if Path(entry["path"]).name == "release_scope.json"
    ]
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_bytes())
    release = _dict_list(previous["items"])[-1]
    assert snapshot == {
        "schema": "h3-context-release-scope-snapshot/1",
        "release_id": "M90-99",
        "release": release["release"],
        "depends_on": release["depends_on"],
        "release_scope_changes": previous["release_scope_changes"],
    }
    assert (root / archive / ".tmp/terminal_items.json").read_bytes() == terminal_before
    historical = cast(dict[str, object], history_lookup(root, archive, "M90-99")["item"])
    assert historical["status"] == "DONE"
    assert restore_check(root, archive)["status"] == "PASS"
    assert "| Wave 0 |" not in (root / "ROADMAP.md").read_text(encoding="utf-8")
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)


def test_retirement_stale_cas_keeps_newer_authority_and_verified_unused_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, previous = _release_repo(tmp_path)
    arguments = _retirement_arguments(root, previous)
    concurrent = deepcopy(previous)
    _dict_list(concurrent["items"])[0]["summary"] = "Newer owner update"
    replacement = (json.dumps(concurrent, ensure_ascii=False, indent=2) + "\n").encode()
    original_render = registry._render_outputs

    def render_after_concurrent_write(index: dict[str, object]) -> dict[Path, bytes]:
        (root / DEFAULT_INDEX_PATH).write_bytes(replacement)
        return original_render(index)

    monkeypatch.setattr(registry, "_render_outputs", render_after_concurrent_write)
    with pytest.raises(RoadmapIndexError):
        main(arguments, root=root)
    assert (root / DEFAULT_INDEX_PATH).read_bytes() == replacement
    archive = Path(".planning/roadmap/archive/release-end")
    assert restore_check(root, archive)["status"] == "PASS"
    assert not (root / "ROADMAP.md").exists()


@pytest.mark.parametrize("drift", [None, "changed", "unknown"])
def test_last_track_retirement_recovers_only_archive_proven_projection_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str | None,
) -> None:
    root, previous = _release_repo(tmp_path)
    release = _dict_list(previous["items"])[-1]
    release["id"] = "M26-99"
    _dict_list(previous["release_scope_changes"])[0]["release_id"] = "M26-99"
    _write_json(root / DEFAULT_INDEX_PATH, previous)
    before = (root / DEFAULT_INDEX_PATH).read_bytes()
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
    view = root / registry.VIEW_DIRECTORY / "M26.md"
    old_view = view.read_bytes()
    arguments = _retirement_arguments(root, previous)
    arguments[arguments.index("--release") + 1] = "M26-99"
    original_write = registry._atomic_write

    def locked_projection(root: Path, relative: Path, raw: bytes, field: str) -> None:
        if relative == Path("ROADMAP.md"):
            raise PermissionError("synthetic locked projection")
        original_write(root, relative, raw, field)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "_atomic_write", locked_projection)
        with pytest.raises(RoadmapIndexError, match="recover with explicit render"):
            main(arguments, root=root)
    current = load_index(root, DEFAULT_INDEX_PATH)
    assert current["items"] == _dict_list(previous["items"])[:-1]
    archive = Path(".planning/roadmap/archive/release-end")
    manifest = json.loads((root / archive / "manifest.json").read_bytes())
    prior_indexes = [
        entry for entry in manifest["entries"] if Path(entry["path"]).name == "active_index.json"
    ]
    assert len(prior_indexes) == 1
    assert (root / archive / prior_indexes[0]["path"]).read_bytes() == before
    assert restore_check(root, archive)["status"] == "PASS"
    if drift == "changed":
        view.write_bytes(old_view + b"Unowned edit.\n")
    elif drift == "unknown":
        (view.parent / "unowned.md").write_bytes(b"Unowned file.\n")
    bound = (root / DEFAULT_INDEX_PATH).read_bytes()
    untouched_view = view.read_bytes()
    with pytest.raises(RoadmapIndexError):
        render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)
    assert view.read_bytes() == untouched_view
    if drift is not None:
        with pytest.raises(RoadmapIndexError):
            render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
        assert view.read_bytes() == untouched_view
        assert (root / DEFAULT_INDEX_PATH).read_bytes() == bound
        return
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=False)
    assert not view.exists()
    assert (root / DEFAULT_INDEX_PATH).read_bytes() == bound
    render_roadmap(root, DEFAULT_INDEX_PATH, "ROADMAP.md", check=True)


def _require_repository_roadmap_context(root: Path) -> None:
    index = root / DEFAULT_INDEX_PATH
    projection = root / "ROADMAP.md"

    def entry_present(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    index_present = entry_present(index)
    projection_present = entry_present(projection)
    if index_present and projection_present:
        return
    if not index_present and not projection_present:
        pytest.skip("local-only ignored roadmap context absent from public source checkout")
    pytest.fail("partial local-only ignored roadmap context: index and projection must both exist")


def _repository_roadmap_context_outcome(root: Path) -> str:
    try:
        _require_repository_roadmap_context(root)
    except pytest.skip.Exception:
        return "skip"
    except pytest.fail.Exception:
        return "fail"
    return "run"


def _dict_list(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast(list[dict[str, object]], value)


def _string_list(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return cast(list[str], value)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _plan(root: Path, name: str) -> str:
    relative = Path(".planning") / f"260813-{name}_PLAN.md"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# {name} plan\n\n## Acceptance criteria\n\n- AC-{name}-01\n")
    return relative.as_posix()


def _archive(root: Path) -> dict[str, object]:
    source = Path(".tmp/roadmap.json")
    _write_json(
        root / source,
        {
            "schema": "h3-context-roadmap-registry/1",
            "items": [
                {
                    "id": "M16-03",
                    "title": "Release audit",
                    "status": "DONE",
                    "depends_on": ["M15-18"],
                }
            ],
        },
    )
    return archive_authorities(root, [source], DEFAULT_ARCHIVE_PATH)


def _index(root: Path) -> dict[str, object]:
    manifest = _archive(root)
    return {
        "schema": ACTIVE_INDEX_SCHEMA,
        "release_scope_changes": [
            {
                "date": "2026-01-01T12:00:00+08:00",
                "release_id": "M16-05",
                "change": "added",
                "item": "RM-18",
                "criterion": "owner_instruction",
                "instruction": "The synthetic owner approved the dependency scope.",
                "note": "Inert original release fixture scope.",
            }
        ],
        "latest_accepted": {
            "id": "M16-03",
            "title": "Release and source audit",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "record_path": ".planning/260813-M16-03_RELEASE_AUDIT_IMPLEMENTATION_RECORD.md",
        },
        "items": [
            {
                "id": "RM-17",
                "lane": "standard",
                "title": "Active roadmap index simplification",
                "status": "IN_PROGRESS",
                "depends_on": ["M16-03"],
                "owner": "RM_GOVERNANCE_OPTIMIZATION",
                "plan_path": _plan(root, "RM-17"),
                "plan_state": "FINALIZED",
                "summary": "Compact active roadmap authority",
                "activation_condition": "active on accepted M16-03",
                "live_record_path": None,
            },
            {
                "id": "RM-18",
                "lane": "standard",
                "title": "Test acceptance simplification",
                "status": "PENDING",
                "depends_on": ["RM-17"],
                "owner": "RM_GOVERNANCE_OPTIMIZATION",
                "plan_path": _plan(root, "RM-18"),
                "plan_state": "FINALIZED",
                "summary": "Simplify test acceptance governance",
                "activation_condition": "waits for RM-17",
                "live_record_path": None,
            },
            {
                "id": "M16-05",
                "lane": "standard",
                "release": {
                    "appetite": 1,
                    "admission": ["owner_instruction"],
                    "frozen_at": "2026-01-01T12:00:00+08:00",
                },
                "title": "Authorized publication",
                "status": "PENDING",
                "depends_on": ["RM-18"],
                "owner": "S10_RECONSTRUCTION_RELEASE",
                "plan_path": None,
                "plan_state": "DRAFT",
                "summary": "Publish the accepted package",
                "activation_condition": "plan required before activation",
                "live_record_path": None,
            },
        ],
        "archives": [
            {
                "path": DEFAULT_ARCHIVE_PATH.as_posix(),
                "manifest_sha256": manifest["manifest_sha256"],
                "item_count": 169,
                "description": "full pre-RM-17 registry and generated projections",
            }
        ],
    }


def _repo(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "repo"
    (root / ".tmp").mkdir(parents=True)
    index = _index(root)
    _write_json(root / DEFAULT_INDEX_PATH, index)
    return root, index


def test_loader_accepts_bounded_active_index_and_pending_placeholder(tmp_path: Path) -> None:
    root, expected = _repo(tmp_path)

    actual = load_index(root, DEFAULT_INDEX_PATH)

    assert actual == expected
    assert actual["schema"] == ACTIVE_INDEX_SCHEMA
    items = _dict_list(actual["items"])
    assert len(items) == 3
    assert all("roadmap_acceptance" not in item for item in items)


def test_loader_rejects_duplicate_members_unknowns_and_unsafe_plan_paths(
    tmp_path: Path,
) -> None:
    root, index = _repo(tmp_path)
    path = root / DEFAULT_INDEX_PATH
    duplicate = path.read_text(encoding="utf-8").replace(
        f'"schema": "{ACTIVE_INDEX_SCHEMA}",',
        f'"schema": "{ACTIVE_INDEX_SCHEMA}",\n  "schema": "{ACTIVE_INDEX_SCHEMA}",',
        1,
    )
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(RoadmapIndexError, match="duplicate JSON member"):
        load_index(root, DEFAULT_INDEX_PATH)

    mutations: list[tuple[dict[str, object], str]] = []
    unknown = deepcopy(index)
    unknown["ledger"] = []
    mutations.append((unknown, "members are not closed"))
    unsafe = deepcopy(index)
    _dict_list(unsafe["items"])[0]["plan_path"] = "../outside.md"
    mutations.append((unsafe, "safe repo-relative"))
    missing = deepcopy(index)
    _dict_list(missing["items"])[0]["plan_path"] = None
    mutations.append((missing, "only PENDING"))
    unknown_status = deepcopy(index)
    _dict_list(unknown_status["items"])[0]["status"] = "DONE"
    mutations.append((unknown_status, "active status"))
    table_injection = deepcopy(index)
    _dict_list(table_injection["items"])[0]["summary"] = "line one\n| injected row"
    mutations.append((table_injection, "single-line"))
    invalid_plan_state = deepcopy(index)
    _dict_list(invalid_plan_state["items"])[0]["plan_state"] = "NOT_REQUIRED"
    mutations.append((invalid_plan_state, "cannot be NOT_REQUIRED"))
    invalid_live_record = deepcopy(index)
    pending = _dict_list(invalid_live_record["items"])[1]
    record = Path(".planning/pending_IMPLEMENTATION_RECORD.md")
    (root / record).write_text("# pending record\n", encoding="utf-8")
    pending["live_record_path"] = record.as_posix()
    mutations.append((invalid_live_record, "cannot own a live record"))

    for value, message in mutations:
        _write_json(path, value)
        with pytest.raises(RoadmapIndexError, match=message):
            load_index(root, DEFAULT_INDEX_PATH)


def test_loader_rejects_missing_or_linked_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, index = _repo(tmp_path)
    first = _dict_list(index["items"])[0]
    plan_path = root / str(first["plan_path"])
    plan_path.unlink()
    with pytest.raises(RoadmapIndexError, match="plan file"):
        load_index(root, DEFAULT_INDEX_PATH)

    plan_path.write_text("# restored\n", encoding="utf-8")
    original = Path.is_symlink

    def selected_link(candidate: Path) -> bool:
        return candidate == plan_path or original(candidate)

    monkeypatch.setattr(Path, "is_symlink", selected_link)
    with pytest.raises(RoadmapIndexError, match="symlink|reparse"):
        load_index(root, DEFAULT_INDEX_PATH)


def test_loader_rejects_duplicate_ids_dangling_dependencies_and_cycles(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    path = root / DEFAULT_INDEX_PATH

    duplicate = deepcopy(index)
    duplicate_items = _dict_list(duplicate["items"])
    duplicate_items.append(deepcopy(duplicate_items[0]))
    dangling = deepcopy(index)
    _dict_list(dangling["items"])[0]["depends_on"] = ["M99-99"]
    cyclic = deepcopy(index)
    _dict_list(cyclic["items"])[0]["depends_on"] = ["RM-18"]

    for value, message in ((duplicate, "duplicate"), (dangling, "dangling"), (cyclic, "cycle")):
        _write_json(path, value)
        with pytest.raises(RoadmapIndexError, match=message):
            load_index(root, DEFAULT_INDEX_PATH)


def test_loader_accepts_archive_catalog_beyond_legacy_sixteen(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    archives = _dict_list(index["archives"])
    for number in range(1, 17):
        source = Path(".tmp/roadmap.json")
        archive_path = Path(f".planning/roadmap/archive/catalog-{number}")
        manifest = archive_authorities(root, [source], archive_path)
        archives.append(
            {
                "path": archive_path.as_posix(),
                "manifest_sha256": manifest["manifest_sha256"],
                "item_count": 1,
                "description": f"synthetic archive {number}",
            }
        )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    loaded = load_index(root, DEFAULT_INDEX_PATH)

    assert len(_dict_list(loaded["archives"])) == 17


def _append_synthetic_archives(root: Path, index: dict[str, object], count: int) -> None:
    archives = _dict_list(index["archives"])
    source = Path(".tmp/roadmap.json")
    for number in range(1, count + 1):
        archive_path = Path(f".planning/roadmap/archive/catalog-{number}")
        manifest = archive_authorities(root, [source], archive_path)
        archives.append(
            {
                "path": archive_path.as_posix(),
                "manifest_sha256": manifest["manifest_sha256"],
                "item_count": 1,
                "description": f"synthetic archive {number}",
            }
        )


def test_loader_and_projections_accept_a_catalog_beyond_the_former_count_ceiling(
    tmp_path: Path,
) -> None:
    root, index = _repo(tmp_path)
    _append_synthetic_archives(root, index, 129)
    _write_json(root / DEFAULT_INDEX_PATH, index)
    index_bytes = (root / DEFAULT_INDEX_PATH).stat().st_size
    assert index_bytes <= MAX_INDEX_BYTES

    loaded = load_index(root, DEFAULT_INDEX_PATH)
    rendered = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)

    assert len(_dict_list(loaded["archives"])) == 130
    assert rendered["status"] == "PASS"
    assert "Catalog: 130 archives" in (root / "ROADMAP.md").read_text(encoding="utf-8")
    catalog_view = root / ".planning/roadmap/views/ARCHIVES.md"
    catalog_lines = catalog_view.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("## ") for line in catalog_lines) == 130
    # The generated catalog view is smaller than the index entries it projects, so the index byte
    # budget, not the projection read bound, is what limits the catalog.
    assert catalog_view.stat().st_size < index_bytes
    argv = ["render", "--index", Path(DEFAULT_INDEX_PATH).as_posix(), "--output", "ROADMAP.md"]
    assert main([*argv, "--check"], root=root) == 0


def test_loader_refuses_a_catalog_that_exceeds_the_index_byte_budget(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    archives = _dict_list(index["archives"])
    number = 0
    while len(json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")) <= MAX_INDEX_BYTES:
        number += 1
        archives.append(
            {
                "path": f".planning/roadmap/archive/never-opened-{number}",
                "manifest_sha256": "c" * 64,
                "item_count": 1,
                "description": "x" * 1_000,
            }
        )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    # The byte budget refuses the index before any archive it names is opened; none of these exist.
    with pytest.raises(RoadmapIndexError, match="active roadmap index has invalid byte length"):
        load_index(root, DEFAULT_INDEX_PATH)


def test_loader_still_refuses_an_empty_archive_catalog(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    index["archives"] = []
    _write_json(root / DEFAULT_INDEX_PATH, index)

    with pytest.raises(RoadmapIndexError, match="archive count is invalid"):
        load_index(root, DEFAULT_INDEX_PATH)


def test_archive_binds_exact_bytes_is_idempotent_and_refuses_drift(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first_path = Path("ROADMAP.md")
    second_path = Path(".planning/roadmap/roadmap.json")
    (root / second_path).parent.mkdir(parents=True)
    (root / first_path).write_text("old roadmap\n", encoding="utf-8")
    _write_json(root / second_path, {"schema": "legacy", "items": []})

    first = archive_authorities(root, [first_path, second_path], DEFAULT_ARCHIVE_PATH)
    second = archive_authorities(root, [first_path, second_path], DEFAULT_ARCHIVE_PATH)

    assert first == second
    assert first["entry_count"] == 2
    for entry in _dict_list(first["entries"]):
        relative = Path(str(entry["path"]))
        assert (root / relative).read_bytes() == (
            root / DEFAULT_ARCHIVE_PATH / relative
        ).read_bytes()
    (root / first_path).write_text("changed\n", encoding="utf-8")
    with pytest.raises(RoadmapIndexError, match="existing archive"):
        archive_authorities(root, [first_path, second_path], DEFAULT_ARCHIVE_PATH)


def test_history_lookup_reads_terminal_item_from_immutable_archive(tmp_path: Path) -> None:
    root, _index_value = _repo(tmp_path)

    result = history_lookup(root, DEFAULT_ARCHIVE_PATH, "M16-03")

    assert result == {
        "schema": "h3-context-roadmap-history/1",
        "status": "PASS",
        "item": {
            "id": "M16-03",
            "title": "Release audit",
            "status": "DONE",
            "depends_on": ["M15-18"],
        },
    }
    with pytest.raises(RoadmapIndexError, match="not found"):
        history_lookup(root, DEFAULT_ARCHIVE_PATH, "M99-99")


def test_history_lookup_prefers_accepted_terminal_item_archive(tmp_path: Path) -> None:
    root, _index_value = _repo(tmp_path)
    source = Path(".tmp/terminal_items.json")
    plan_name = "260813-RM-17_ACTIVE_ROADMAP_INDEX_SIMPLIFICATION_PLAN.md"
    _write_json(
        root / source,
        {
            "schema": "h3-context-roadmap-terminal-items/1",
            "items": [
                {
                    "id": "RM-17",
                    "title": "Active roadmap index simplification",
                    "status": "DONE",
                    "depends_on": ["M16-03"],
                    "owner": "RM_GOVERNANCE_OPTIMIZATION",
                    "plan_path": f".planning/{plan_name}",
                    "commit": "c" * 40,
                    "tree": "d" * 40,
                    "record_path": ".planning/260813-RM-17_IMPLEMENTATION_RECORD.md",
                }
            ],
        },
    )
    archive = Path(".planning/roadmap/archive/rm17-closeout")
    archive_authorities(root, [source], archive)

    result = history_lookup(root, archive, "RM-17")

    assert result["status"] == "PASS"
    assert result["item"]["id"] == "RM-17"  # type: ignore[index]
    assert result["item"]["status"] == "DONE"  # type: ignore[index]


def test_render_is_compact_closed_projection_set_and_check_is_read_only(tmp_path: Path) -> None:
    root, _index_value = _repo(tmp_path)

    first = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)
    before = (root / "ROADMAP.md").read_bytes()
    second = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=True)

    assert first == second
    assert first["item_count"] == 3
    assert first["projection_count"] == 3
    text = before.decode("utf-8")
    assert len(text.splitlines()) < 80
    assert len(before) <= 12 * 1024
    assert max(map(len, text.splitlines())) <= 320
    assert "## Current Handoff" in text
    assert "## Active Execution" in text
    assert "## Chain Summary" in text
    assert "## Eligible Frontier" in text
    assert "## Recent Archives" in text
    assert "## Dependency Spine" not in text
    assert "roadmap_acceptance" not in text
    assert (root / ".planning/roadmap/views/LEGACY_RELEASE.md").is_file()
    assert (root / ".planning/roadmap/views/ARCHIVES.md").is_file()

    (root / "ROADMAP.md").write_text("drift\n", encoding="utf-8")
    drifted = (root / "ROADMAP.md").read_bytes()
    with pytest.raises(RoadmapIndexError, match="drift"):
        render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=True)
    assert (root / "ROADMAP.md").read_bytes() == drifted

    render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)
    unexpected = root / ".planning/roadmap/views/UNEXPECTED.md"
    unexpected.write_text("generated drift\n", encoding="utf-8")
    with pytest.raises(RoadmapIndexError, match="unexpected owned"):
        render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=True)


def test_render_partitions_each_item_into_exactly_one_chain_view(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    _dict_list(index["items"]).append(
        {
            "id": "M17-01",
            "lane": "standard",
            "title": "Synthetic M17 item",
            "status": "PENDING",
            "depends_on": ["M16-03"],
            "owner": "S8_CONTEXT_RECONSTRUCTION",
            "plan_path": _plan(root, "M17-01"),
            "plan_state": "FINALIZED",
            "summary": "Exercise deterministic chain ownership",
            "activation_condition": "eligible only; explicit activation remains required",
            "live_record_path": None,
        }
    )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    result = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)

    views = list((root / ".planning/roadmap/views").glob("*.md"))
    item_occurrences = sum(path.read_text(encoding="utf-8").count("## M17-01 —") for path in views)
    assert result["projection_count"] == 4
    assert item_occurrences == 1
    assert "dependency eligibility grants no activation authority" not in (
        root / "ROADMAP.md"
    ).read_text(encoding="utf-8")


def test_render_routes_m24_items_only_to_the_m24_chain_view(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    _dict_list(index["items"]).append(
        {
            "id": "M24-01",
            "lane": "standard",
            "title": "Synthetic M24 item",
            "status": "PENDING",
            "depends_on": ["M16-03"],
            "owner": "S19_PRODUCTION_CLIP_PREVIEW",
            "plan_path": _plan(root, "M24-01"),
            "plan_state": "DRAFT",
            "summary": "Exercise the dedicated M24 chain projection",
            "activation_condition": "eligible only; explicit activation remains required",
            "live_record_path": None,
        }
    )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    result = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)

    heading = "## M24-01 — Synthetic M24 item"
    views = list((root / ".planning/roadmap/views").glob("*.md"))
    assert result["projection_count"] == 4
    assert heading in (root / ".planning/roadmap/views/M24.md").read_text(encoding="utf-8")
    assert heading not in (root / ".planning/roadmap/views/LEGACY_RELEASE.md").read_text(
        encoding="utf-8"
    )
    assert sum(heading in path.read_text(encoding="utf-8") for path in views) == 1


def test_render_routes_m25_items_only_to_the_m25_chain_view(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    _dict_list(index["items"]).append(
        {
            "id": "M25-01",
            "lane": "standard",
            "title": "Synthetic M25 item",
            "status": "PENDING",
            "depends_on": ["M16-03"],
            "owner": "S19_AUTHORING_PREVIEW_CONTRACT",
            "plan_path": _plan(root, "M25-01"),
            "plan_state": "DRAFT",
            "summary": "Exercise the dedicated M25 chain projection",
            "activation_condition": "eligible only; explicit activation remains required",
            "live_record_path": None,
        }
    )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)

    heading = "## M25-01 — Synthetic M25 item"
    views = list((root / ".planning/roadmap/views").glob("*.md"))
    assert heading in (root / ".planning/roadmap/views/M25.md").read_text(encoding="utf-8")
    assert heading not in (root / ".planning/roadmap/views/LEGACY_RELEASE.md").read_text(
        encoding="utf-8"
    )
    assert sum(heading in path.read_text(encoding="utf-8") for path in views) == 1


def test_m26_has_a_dedicated_projection_and_cross_chain_release_waves(tmp_path: Path) -> None:
    root, index = _repo(tmp_path)
    _dict_list(index["items"]).append(
        {
            "id": "M26-00",
            "lane": "standard",
            "title": "Synthetic M26 item",
            "status": "PENDING",
            "depends_on": ["M16-03"],
            "owner": "S20_DURATION_CONTRACT",
            "plan_path": _plan(root, "M26-00"),
            "plan_state": "FINALIZED",
            "summary": "Exercise the dedicated M26 chain projection",
            "activation_condition": "eligible only; explicit activation remains required",
            "live_record_path": None,
        }
    )
    _write_json(root / DEFAULT_INDEX_PATH, index)

    rendered = render_roadmap(root, DEFAULT_INDEX_PATH, Path("ROADMAP.md"), check=False)

    assert _track("M26-00") == "M26"
    assert rendered["projection_count"] == 4
    assert (root / ".planning/roadmap/views/M26.md").is_file()

    items: list[dict[str, object]] = [
        {"id": "M23-51", "status": "BLOCKED", "depends_on": []},
        {"id": "M26-00", "status": "PENDING", "depends_on": []},
        {"id": "M26-01", "status": "PENDING", "depends_on": ["M26-00"]},
        {"id": "M26-02", "status": "PENDING", "depends_on": ["M26-01"]},
        {"id": "M26-03", "status": "PENDING", "depends_on": ["M26-02"]},
        {
            "id": "M26-04",
            "status": "PENDING",
            "depends_on": ["M26-03", "M23-51"],
        },
        {"id": "M26-05", "status": "PENDING", "depends_on": ["M26-03", "M26-04"]},
        {"id": "M25-16", "status": "PENDING", "depends_on": ["M26-05"]},
        {"id": "M16-05", "status": "BLOCKED", "depends_on": ["M25-16"]},
    ]
    assert _dependency_waves(items, target_id="M16-05") == [
        ["M23-51", "M26-00"],
        ["M26-01"],
        ["M26-02"],
        ["M26-03"],
        ["M26-04"],
        ["M26-05"],
        ["M25-16"],
        ["M16-05"],
    ]


def test_coordination_plan_paths_are_deduplicated_and_repo_relative() -> None:
    items: list[dict[str, object]] = [
        {"activation_condition": "Coordination: 260903-M23_M25_M26_PRE_RELEASE_PLAN.md. First."},
        {"activation_condition": "No separate coordination authority."},
        {"activation_condition": "Coordination: 260903-M23_M25_M26_PRE_RELEASE_PLAN.md. Second."},
    ]

    assert _coordination_plan_paths(items) == [".planning/260903-M23_M25_M26_PRE_RELEASE_PLAN.md"]


def test_restore_check_verifies_archive_in_owned_temp_and_cleans(tmp_path: Path) -> None:
    root, _index_value = _repo(tmp_path)
    before = set((root / ".tmp").iterdir())

    result = restore_check(root, DEFAULT_ARCHIVE_PATH)

    assert result["status"] == "PASS"
    assert result["cleanup"] == "PASS"
    assert set((root / ".tmp").iterdir()) == before


def test_cli_has_only_bounded_active_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _index_value = _repo(tmp_path)
    commands = (
        ["render", "--index", DEFAULT_INDEX_PATH.as_posix(), "--output", "ROADMAP.md"],
        [
            "render",
            "--index",
            DEFAULT_INDEX_PATH.as_posix(),
            "--output",
            "ROADMAP.md",
            "--check",
        ],
        ["history", "--archive", DEFAULT_ARCHIVE_PATH.as_posix(), "--item", "M16-03"],
        ["restore-check", "--archive", DEFAULT_ARCHIVE_PATH.as_posix()],
    )
    for command in commands:
        assert main(command, root=root) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_repository_roadmap_context_requires_complete_pair(tmp_path: Path) -> None:
    absent_root = tmp_path / "absent"
    absent_root.mkdir()
    with pytest.raises(
        pytest.skip.Exception,
        match="local-only ignored roadmap context absent from public source checkout",
    ):
        _require_repository_roadmap_context(absent_root)

    index_only_root = tmp_path / "index-only"
    _write_json(index_only_root / DEFAULT_INDEX_PATH, {})
    with pytest.raises(pytest.fail.Exception, match="partial local-only ignored roadmap context"):
        _require_repository_roadmap_context(index_only_root)

    projection_only_root = tmp_path / "projection-only"
    projection_only_root.mkdir()
    (projection_only_root / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="partial local-only ignored roadmap context"):
        _require_repository_roadmap_context(projection_only_root)


@pytest.mark.parametrize(("dangling_count", "expected"), ((1, "fail"), (2, "run")))
def test_repository_roadmap_context_treats_dangling_entries_as_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dangling_count: int,
    expected: str,
) -> None:
    root = tmp_path / f"dangling-{dangling_count}"
    root.mkdir()
    entries = (root / DEFAULT_INDEX_PATH, root / "ROADMAP.md")
    dangling = set(entries[:dangling_count])
    original_lstat = Path.lstat
    present_stat = root.lstat()

    def dangling_lstat(path: Path) -> os.stat_result:
        if path in dangling:
            return present_stat
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", dangling_lstat)
    assert all(not entry.exists() for entry in entries)
    assert _repository_roadmap_context_outcome(root) == expected
    if dangling_count == 2:
        with pytest.raises(RoadmapIndexError, match="regular nonlink"):
            load_index(root, DEFAULT_INDEX_PATH)


@pytest.mark.parametrize("entry_kind", ("directory", "corrupt", "link"))
def test_repository_roadmap_context_routes_present_corruption_to_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    root = tmp_path / entry_kind
    index = root / DEFAULT_INDEX_PATH
    projection = root / "ROADMAP.md"
    index.parent.mkdir(parents=True)
    if entry_kind == "directory":
        index.mkdir()
        projection.mkdir()
        expected = "regular nonlink"
    else:
        index.write_text("not-json\n", encoding="utf-8")
        projection.write_text("# Roadmap\n", encoding="utf-8")
        expected = "symlink|reparse" if entry_kind == "link" else "invalid JSON"
    if entry_kind == "link":
        original = Path.is_symlink

        def selected_link(path: Path) -> bool:
            return path == index or original(path)

        monkeypatch.setattr(Path, "is_symlink", selected_link)

    assert _repository_roadmap_context_outcome(root) == "run"
    with pytest.raises(RoadmapIndexError, match=expected):
        load_index(root, DEFAULT_INDEX_PATH)


def test_public_tool_has_no_product_runtime_or_external_activity() -> None:
    text = (ROOT / "scripts/roadmap_registry.py").read_text(encoding="utf-8")
    forbidden = (
        "comfyui_h3_context",
        "requests",
        "urllib.request",
        "httpx",
        "aiohttp",
        "subprocess",
        "socket",
    )
    assert all(f"import {name}" not in text and f"from {name}" not in text for name in forbidden)
