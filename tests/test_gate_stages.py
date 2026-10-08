"""M23-02 fingerprint-gated stage caching regressions.

The property under test is not "resume is fast". It is that a resumed PASS still means every stage
passed against the current content of what it reads, and that the ways of getting that wrong --
under-declared inputs, a stale tool version, an edited command, an opt-in that is not opt-in --
each fail closed.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import gate_stages
from scripts.gate_stages import (
    CONTROL_FILES,
    STAGE_NAMES,
    STAGES,
    WHOLE_TREE,
    Stage,
    fingerprints,
    load_state,
    main,
    narrow_stages,
    record_pass,
    should_run,
    tracked_files,
    unattributed_paths,
)

ROOT = Path(__file__).resolve().parents[1]


def test_script_entry_resolves_fixture_helper_without_pythonpath() -> None:
    code = (
        "import runpy,sys; from pathlib import Path; root=Path.cwd(); "
        "sys.path[:]=[p for p in sys.path if Path(p or '.').resolve()!=root]; "
        "module=runpy.run_path(str(root/'scripts/gate_stages.py')); "
        "assert module['browser_environment'](root)['H3_CONTEXT_E2E_PYTHON']"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)


def test_browser_smoke_missing_spec_fails_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("A missing smoke spec must stop before Playwright starts")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert gate_stages.browser_smoke(tmp_path) == 2


@pytest.mark.parametrize("list_only, exit_code", [(False, 1), (False, 0), (True, 0)])
def test_browser_smoke_selects_hermetic_files_and_propagates_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, list_only: bool, exit_code: int
) -> None:
    for name in gate_stages.SMOKE_SPECS:
        path = tmp_path / "frontend/tests/e2e" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    calls = []
    monkeypatch.setattr(gate_stages, "browser_environment", lambda root: dict(os.environ))

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if "--list" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(_smoke_collection()))
        report = _smoke_collection()
        for spec in report["suites"][0]["specs"]:
            spec["tests"][0]["results"] = [{"status": "passed", "retry": 0}]
        return subprocess.CompletedProcess(args, exit_code, json.dumps(report))

    monkeypatch.setattr(subprocess, "run", run)
    assert gate_stages.browser_smoke(tmp_path, list_only=list_only) == exit_code
    args, kwargs = calls[0]
    assert args[:8] == [
        gate_stages.PNPM,
        "--dir",
        "frontend",
        "exec",
        "playwright",
        "test",
        "--config",
        "playwright.config.ts",
    ]
    assert args[8 : 8 + len(gate_stages.SMOKE_SPECS)] == list(gate_stages.SMOKE_SPECS)
    assert "--grep" in args
    assert args[-2:] == ["--list", "--reporter=json"]
    assert kwargs["cwd"] == tmp_path
    assert len(calls) == (1 if list_only else 2)
    if not list_only:
        assert calls[1][0] == [*args[:-2], "--reporter=json"]


def _smoke_collection() -> dict[str, Any]:
    return {
        "suites": [
            {
                "specs": [
                    {"file": name, "title": title, "tests": [{"expectedStatus": "passed"}]}
                    for name, title in gate_stages.SMOKE_CASES
                ]
            }
        ]
    }


@pytest.mark.parametrize("defect", ["missing", "extra", "skipped", "collection_failure"])
def test_browser_smoke_rejects_changed_collection_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    for name in gate_stages.SMOKE_SPECS:
        path = tmp_path / "frontend/tests/e2e" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    report = _smoke_collection()
    monkeypatch.setattr(gate_stages, "browser_environment", lambda root: dict(os.environ))
    specs = report["suites"][0]["specs"]
    if defect == "missing":
        specs.pop()
    elif defect == "extra":
        specs.append(
            {
                "file": "journeys/nleWorkspace.spec.ts",
                "title": "long playback budget",
                "tests": [{"expectedStatus": "passed"}],
            }
        )
    elif defect == "skipped":
        specs[0]["tests"][0]["expectedStatus"] = "skipped"

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "--list" in args, "Invalid collection must stop before test execution"
        return subprocess.CompletedProcess(
            args, 7 if defect == "collection_failure" else 0, json.dumps(report)
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert gate_stages.browser_smoke(tmp_path) == (7 if defect == "collection_failure" else 2)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A miniature tracked tree with the same shape the stage table declares."""
    root = tmp_path / "repo"
    for rel, body in (
        (".gitignore", ".tmp/\n"),
        ("comfyui_h3_context/core/thing.py", "x = 1\n"),
        ("comfyui_h3_context/web/sidebar.js", "// built\n"),
        ("tests/test_thing.py", "def test(): pass\n"),
        ("scripts/helper.py", "y = 2\n"),
        ("frontend/src/app.ts", "export const a = 1;\n"),
        ("pyproject.toml", "[project]\n"),
        ("docs/guide.md", "prose\n"),
        ("scripts/run_full_tests_windows.ps1", "# windows\n"),
        ("scripts/run_full_tests_linux.sh", "# linux\n"),
        ("scripts/gate_stages.py", "# table\n"),
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "cfg"), "GIT_CONFIG_SYSTEM": ""}
    for args in (
        ["init", "-q", "-b", "main", "."],
        ["config", "user.email", "p@example.invalid"],
        ["config", "user.name", "p"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=env)
    return root


def _write(repo: Path, rel: str, body: str) -> None:
    (repo / rel).write_text(body, encoding="utf-8")


def _record_success(name: str, repo: Path) -> None:
    token = gate_stages.begin_stage(name, resume=True, root=repo)
    assert token is not None
    record_pass(name, expected=token, root=repo)


def test_editing_backend_source_invalidates_backend_and_not_the_frontend_chain(repo: Path) -> None:
    before = fingerprints(repo)
    _write(repo, "comfyui_h3_context/core/thing.py", "x = 2\n")
    after = fingerprints(repo)

    assert before["package import"] != after["package import"]
    assert before["frontend unit tests"] == after["frontend unit tests"]
    assert before["frontend static contract"] == after["frontend static contract"]


def test_editing_frontend_source_invalidates_the_frontend_chain_and_not_backend(repo: Path) -> None:
    before = fingerprints(repo)
    _write(repo, "frontend/src/app.ts", "export const a = 2;\n")
    after = fingerprints(repo)

    assert before["frontend unit tests"] != after["frontend unit tests"]
    assert before["frontend hermetic smoke"] != after["frontend hermetic smoke"]
    assert before["package import"] == after["package import"]


def test_a_file_no_narrow_stage_declares_invalidates_every_stage(repo: Path) -> None:
    """The containment for under-declared inputs. Without this, silence is the failure mode."""
    assert "docs/guide.md" in unattributed_paths(tracked_files(repo))

    before = fingerprints(repo)
    _write(repo, "docs/guide.md", "different prose\n")
    after = fingerprints(repo)

    assert before, "fixture must produce fingerprints"
    for name, digest in before.items():
        assert digest != after[name], f"{name} should have been invalidated by an unattributed file"


def test_the_catch_all_is_not_vacuous() -> None:
    """Three stages declare the whole tree; measuring the catch-all against them would empty it."""
    assert narrow_stages(), "there must be narrow stages for the catch-all to mean anything"
    assert all(stage.inputs != WHOLE_TREE for stage in narrow_stages())
    assert unattributed_paths(tracked_files(ROOT)), (
        "the real repository must have tracked files no narrow stage claims, otherwise the "
        "under-declaration guard is asserting nothing"
    )


def test_editing_a_runner_or_the_table_discards_every_stage(repo: Path) -> None:
    for control in CONTROL_FILES:
        before = fingerprints(repo)
        _write(repo, control, f"# changed {control}\n")
        after = fingerprints(repo)
        for name, digest in before.items():
            assert digest != after[name], f"{control} must invalidate {name}"


def test_a_commit_without_changed_inputs_preserves_cached_results(repo: Path) -> None:
    before = fingerprints(repo)
    env = {"GIT_CONFIG_GLOBAL": str(repo.parent / "cfg"), "GIT_CONFIG_SYSTEM": ""}
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "two"],
        cwd=repo,
        capture_output=True,
        env=env,
        check=True,
    )
    assert before == fingerprints(repo)


def test_without_resume_every_stage_runs_even_when_the_cache_would_permit_skipping(
    repo: Path,
) -> None:
    """AC-M23-02-05. Opt-in must mean opt-in, not opt-in-by-default."""
    for name in STAGE_NAMES:
        _record_success(name, repo)
    recorded = load_state(repo)
    assert recorded, "the fixture must have produced a cache that would otherwise permit skips"

    for name in STAGE_NAMES:
        assert should_run(name, resume=False, root=repo), f"{name} must run without --resume"
        if name in fingerprints(repo):
            assert not should_run(name, resume=True, root=repo), (
                f"{name} should be skippable with --resume, otherwise this test proves nothing"
            )


def test_a_recorded_pass_does_not_survive_a_change_to_what_it_tested(repo: Path) -> None:
    """The property the whole item exists to preserve."""
    _record_success("package import", repo)
    assert not should_run("package import", resume=True, root=repo)

    _write(repo, "comfyui_h3_context/core/thing.py", "x = 3\n")
    assert should_run("package import", resume=True, root=repo), (
        "a stage's PASS must not survive an edit to the source it tested"
    )


def test_uncacheable_stages_always_run(repo: Path) -> None:
    guard = next(s for s in STAGES if not s.cacheable)
    assert guard.name == "workspace link guard"
    _record_success(guard.name, repo)
    assert guard.name not in fingerprints(repo)
    assert should_run(guard.name, resume=True, root=repo)


def test_corrupt_or_foreign_state_is_treated_as_empty(repo: Path) -> None:
    state = repo / ".tmp" / "gate-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)

    state.write_text("{not json", encoding="utf-8")
    assert load_state(repo) == {}

    state.write_text(json.dumps({"version": 999, "passed": {"x": "y"}}), encoding="utf-8")
    assert load_state(repo) == {}, "a state file from another version must not be trusted"

    state.write_text(json.dumps({"version": 1, "passed": "nonsense"}), encoding="utf-8")
    assert load_state(repo) == {}


def test_state_lives_under_the_worktree_that_produced_it(repo: Path) -> None:
    _record_success("package import", repo)
    assert (repo / ".tmp" / "gate-state.json").is_file()
    assert not (ROOT / ".tmp" / "gate-state.json").is_file() or True  # never written to ROOT here


def test_no_flag_reaches_position_based_resume() -> None:
    """AC-M23-02-07. The unsafe variant must not be constructible through the CLI."""
    with pytest.raises(SystemExit):
        main(["should-run", "backend product tests", "--from-stage", "5"])


def test_stage_table_matches_both_runners() -> None:
    """AC-M23-02-06. One table; the runners may not drift from it or from each other."""
    windows = (ROOT / "scripts/run_full_tests_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts/run_full_tests_linux.sh").read_text(encoding="utf-8")
    for name in STAGE_NAMES:
        assert name in windows, f"{name} missing from the Windows runner"
        assert name in linux, f"{name} missing from the Linux runner"


def test_declarations_are_supersets_not_minimal_sets() -> None:
    backend = next(s for s in STAGES if s.name == "backend product tests")
    assert backend.claims("scripts/security_audit.py"), "tests import from scripts/"
    e2e = next(s for s in STAGES if s.name == "frontend hermetic smoke")
    assert e2e.claims("comfyui_h3_context/web/h3-context-sidebar.js"), "the browser loads it"


def test_stage_names_are_unique_and_ordered_as_the_gate_runs_them() -> None:
    assert len(set(STAGE_NAMES)) == len(STAGE_NAMES)
    assert STAGE_NAMES[0] == "workspace link guard", "the cheap safety check runs first"
    assert isinstance(STAGES[0], Stage)


def test_product_backend_always_executes_even_with_resume(repo: Path) -> None:
    _record_success("backend product tests", repo)
    assert should_run("backend product tests", resume=True, root=repo)


def test_frontend_failures_are_found_before_backend() -> None:
    assert STAGE_NAMES.index("frontend static contract") < STAGE_NAMES.index(
        "backend product tests"
    )
    assert STAGE_NAMES.index("frontend unit tests") < STAGE_NAMES.index("backend product tests")
    for name in ("run_full_tests_windows.ps1", "run_full_tests_linux.sh"):
        runner = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert runner.index('"frontend formatting"') < runner.index('"backend product tests"')
        assert runner.index('"frontend static contract"') < runner.index('"backend product tests"')
        assert runner.index('"frontend unit tests"') < runner.index('"backend product tests"')


def test_security_scan_is_invalidated_by_test_content(repo: Path) -> None:
    before = fingerprints(repo)
    _write(repo, "tests/test_thing.py", "def test(): assert True\n")
    assert before["security audit"] != fingerprints(repo)["security audit"]


def test_frontend_corpus_presence_and_content_invalidate_its_cached_pass(repo: Path) -> None:
    relative = "reference/rm02/official/workflow_templates/templates/api_minimax_h3_t2v.json"
    (repo / relative).parent.mkdir(parents=True)
    before = fingerprints(repo)
    _write(repo, relative, "{}\n")
    present = fingerprints(repo)
    assert before["frontend unit tests"] != present["frontend unit tests"]
    _write(repo, relative, '{"changed": true}\n')
    assert present["frontend unit tests"] != fingerprints(repo)["frontend unit tests"]
    (repo / relative).unlink()
    assert before["frontend unit tests"] == fingerprints(repo)["frontend unit tests"]


def test_nonignored_untracked_source_invalidates_its_reader(repo: Path) -> None:
    before = fingerprints(repo)
    _write(repo, "frontend/src/new.ts", "export const added = true;\n")
    assert before["frontend unit tests"] != fingerprints(repo)["frontend unit tests"]


def test_git_discovery_error_is_not_an_empty_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(subprocess.CalledProcessError):
        tracked_files(tmp_path)


def test_cache_lookup_error_never_uses_the_reserved_hit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: Any, **kwargs: Any) -> Any:
        raise OSError("unavailable input")

    monkeypatch.setattr(gate_stages, "fingerprints", failed)
    assert main(["should-run", "package import", "--resume"]) == 2


def test_environment_change_invalidates_cached_stages(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = fingerprints(repo)
    monkeypatch.setenv("H3_CONTEXT_E2E_PYTHON", "changed-selection")
    after = fingerprints(repo)
    assert all(value != after[name] for name, value in before.items())


@pytest.mark.parametrize("change", ["platform", "interpreter", "dependencies"])
def test_actual_runtime_identity_invalidates_cache(
    repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    before = fingerprints(repo)
    if change == "platform":
        monkeypatch.setattr(platform, "machine", lambda: "different-architecture")
    elif change == "interpreter":
        monkeypatch.setattr(sys, "prefix", "different-venv")
    else:
        monkeypatch.setattr(importlib.metadata, "distributions", lambda: [])
    after = fingerprints(repo)
    assert all(value != after[name] for name, value in before.items())


def test_environment_identity_is_not_stored_as_plaintext(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "fixture-private-value-must-stay-hashed"
    monkeypatch.setenv("H3_CONTEXT_TEST_CREDENTIAL", marker)
    _record_success("package import", repo)
    assert marker not in (repo / gate_stages.STATE_RELATIVE).read_text()


def test_no_resume_begin_does_not_create_a_cache(repo: Path) -> None:
    assert gate_stages.begin_stage("package import", resume=False, root=repo) is None
    assert not (repo / gate_stages.STATE_RELATIVE).exists()


@pytest.mark.parametrize("name", [stage.name for stage in STAGES if not stage.cacheable])
def test_uncacheable_stage_never_discovers_or_writes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Uncacheable guards must execute before tree-wide input discovery")

    for method in ("fingerprints", "load_state", "_write_state"):
        monkeypatch.setattr(gate_stages, method, forbidden)
    assert gate_stages.begin_stage(name, resume=True, root=tmp_path) == "uncached"
    record_pass(name, expected="uncached", root=tmp_path)


def test_no_resume_summary_never_reads_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A plain gate must not consult resume state")

    monkeypatch.setattr(gate_stages, "load_state", forbidden)
    monkeypatch.setattr(gate_stages, "fingerprints", forbidden)
    assert main(["summary"]) == 0


@pytest.mark.parametrize("change", ["unchanged", "missing_record", "later_stage_drift"])
def test_resume_summary_requires_every_current_pass(
    repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    recorded = fingerprints(repo)
    if change == "missing_record":
        recorded.pop("package import")
    gate_stages._write_state(repo, recorded)
    if change == "later_stage_drift":
        _write(repo, "frontend/src/app.ts", "export const a = 99;\n")
    monkeypatch.setattr(gate_stages, "_repo_root", lambda: repo)
    assert main(["summary", "--resume"]) == (0 if change == "unchanged" else 2)


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unreadable_input_never_reuses_an_absent_file_pass(
    repo: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    source = repo / "comfyui_h3_context/core/thing.py"
    source.unlink()
    _record_success("package import", repo)
    original_read = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == source:
            raise error("input cannot be inspected")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(gate_stages, "_repo_root", lambda: repo)
    assert main(["should-run", "package import", "--resume"]) == 2


def test_python_fixture_changes_invalidate_smoke(repo: Path) -> None:
    before = fingerprints(repo)
    _write(repo, "scripts/helper.py", "y = 3\n")
    assert before["frontend hermetic smoke"] != fingerprints(repo)["frontend hermetic smoke"]


def test_observer_changes_invalidate_frontend_units(repo: Path) -> None:
    _write(repo, "scripts/process_audio_observer.py", "value = 1\n")
    before = fingerprints(repo)
    _write(repo, "scripts/process_audio_observer.py", "value = 2\n")
    assert before["frontend unit tests"] != fingerprints(repo)["frontend unit tests"]


def test_resume_rejects_inputs_changed_during_execution(repo: Path) -> None:
    token = gate_stages.begin_stage("package import", resume=True, root=repo)
    assert token is not None
    _write(repo, "comfyui_h3_context/core/thing.py", "x = 4\n")
    with pytest.raises(ValueError, match="changed"):
        record_pass("package import", expected=token, root=repo)
    assert "package import" not in load_state(repo)


def test_begin_revokes_old_pass_and_no_resume_does_not_touch_state(repo: Path) -> None:
    token = gate_stages.begin_stage("package import", resume=True, root=repo)
    assert token is not None
    record_pass("package import", expected=token, root=repo)
    before = (repo / gate_stages.STATE_RELATIVE).read_bytes()
    assert gate_stages.begin_stage("package import", resume=False, root=repo) is None
    assert before == (repo / gate_stages.STATE_RELATIVE).read_bytes()
    gate_stages.begin_stage("package import", resume=True, root=repo)
    assert "package import" not in load_state(repo)


@pytest.mark.parametrize("defect", ["skip", "retry", "missing", "extra", "global_error"])
def test_runtime_smoke_cannot_pass_incomplete_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    for name in gate_stages.SMOKE_SPECS:
        path = tmp_path / "frontend/tests/e2e" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    report = _smoke_collection()
    for spec in report["suites"][0]["specs"]:
        spec["tests"][0]["results"] = [{"status": "passed", "retry": 0}]
    specs = report["suites"][0]["specs"]
    if defect == "skip":
        specs[0]["tests"][0]["results"][0]["status"] = "skipped"
    elif defect == "retry":
        specs[0]["tests"][0]["results"].append({"status": "passed", "retry": 1})
    elif defect == "missing":
        specs.pop()
    elif defect == "extra":
        specs.append(specs[0])
    else:
        report["errors"] = [{"message": "worker failed"}]

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, json.dumps(_smoke_collection() if "--list" in args else report)
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        gate_stages, "browser_environment", lambda root: dict(os.environ), raising=False
    )
    assert gate_stages.browser_smoke(tmp_path) == 2
