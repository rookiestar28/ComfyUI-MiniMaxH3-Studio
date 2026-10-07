"""RM-17 bounded active-roadmap index and archive regressions."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

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


def test_active_index_schema_is_v2() -> None:
    assert ACTIVE_INDEX_SCHEMA == "h3-context-active-roadmap-index/2"


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
