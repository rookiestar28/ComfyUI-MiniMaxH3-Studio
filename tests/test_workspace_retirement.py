"""Manifest-bound workspace retirement: admission, effects and refusals on real filesystems.

Every case builds its own Git repository under ``tmp_path`` with a ``.tmp`` scratch directory and
retires entries there.  Links, locks, open handles and hard links are real -- the defect classes
this tool guards against come from real filesystem behaviour that mocks would paper over.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import scripts.workspace_retirement as retirement
from scripts.workspace_retirement import (
    Context,
    ManifestError,
    Refused,
    apply_entry,
    entry_components,
    observe,
    preview,
    validate_manifest,
    verify,
)

EXCLUDED = ["node_modules", "__pycache__"]
MARKERS = ["pyvenv.cfg"]


def _run(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _git(*args: str, cwd: Path) -> str:
    return _run(
        "git",
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        f"core.hooksPath={os.devnull}",
        *args,
        cwd=cwd,
    )


def _junction(link: Path, target: Path) -> bool:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and os.path.lexists(link)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=root)
    (root / ".gitignore").write_bytes(b".tmp/\n.receipts/\nignored/\n")
    (root / "seed.txt").write_bytes(b"seed\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)
    (root / ".tmp").mkdir()
    (root / ".receipts").mkdir()
    return root


def _worktree(workspace: Path, name: str) -> Path:
    path = workspace / ".tmp" / name
    _git("worktree", "add", "-q", "--detach", str(path), cwd=workspace)
    return path


def _plain(workspace: Path, name: str) -> Path:
    path = workspace / ".tmp" / name
    (path / "out").mkdir(parents=True)
    (path / "out" / "result.txt").write_bytes(b"result\n")
    return path


def _refs(repo: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)


def _registered(workspace: Path) -> list[str]:
    listing = _git("worktree", "list", "--porcelain", cwd=workspace)
    return [
        os.path.normcase(os.path.normpath(line[len("worktree ") :]))
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]


def _is_registered(workspace: Path, path: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(path))) in _registered(workspace)


def _preservation(workspace: Path, entry_id: str, digest: str, status: str) -> dict[str, str]:
    receipt = {
        "schema": retirement.PRESERVATION_RECEIPT_SCHEMA,
        "entry_id": entry_id,
        "content_digest": digest,
        "status": status,
    }
    raw = json.dumps(receipt).encode()
    path = workspace / ".receipts" / f"{entry_id}.preservation.json"
    path.write_bytes(raw)
    return {
        "receipt": f".receipts/{entry_id}.preservation.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _entry(
    workspace: Path,
    name: str,
    kind: str,
    disposition: str = "REMOVE_INTEGRATED",
    *,
    status: str = "RESTORE_VERIFIED",
    path: str | None = None,
) -> dict[str, Any]:
    relative = path or f".tmp/{name}"
    entry: dict[str, Any] = {
        "id": name.replace("/", "-"),
        "path": relative,
        "kind": kind,
        "disposition": disposition,
        "rationale": "fixture",
        "identity": None,
        "content_digest": None,
        "git": None,
        "preservation": None,
        "citations": 0,
    }
    if disposition in retirement.KEEPS:
        return entry
    seen = observe(workspace.joinpath(*relative.split("/")), kind, EXCLUDED, MARKERS)
    entry["identity"] = seen.identity
    entry["content_digest"] = seen.content_digest
    entry["git"] = seen.git
    if disposition != "REMOVE_DISPOSABLE":
        entry["preservation"] = _preservation(workspace, entry["id"], seen.content_digest, status)
    return entry


def _quiet_processes() -> list[str]:
    return ["1\tC:\\Windows\\System32\\idle.exe\tidle"]


def _context(workspace: Path, entries: list[dict[str, Any]], run_id: str = "run1") -> Context:
    manifest = {
        "schema": retirement.RETIREMENT_MANIFEST_SCHEMA,
        "run_id": run_id,
        "receipts": ".receipts",
        "excluded_dir_names": EXCLUDED,
        "excluded_dir_markers": MARKERS,
        "entries": entries,
    }
    raw = json.dumps(manifest).encode()
    (workspace / "manifest.json").write_bytes(raw)
    return Context(
        workspace=workspace,
        manifest=validate_manifest(raw),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        process_reader=_quiet_processes,
    )


@pytest.fixture
def recorded_commands(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Every subprocess argv the module runs, so a test can prove what it never ran."""
    calls: list[list[str]] = []
    original = subprocess.run

    def recording(args: Any, *rest: Any, **kwargs: Any) -> Any:
        calls.append([str(a) for a in args])
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    yield calls


# ---------------------------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".tmp",
        ".tmp/",
        "src/x",
        ".tmp-other/x",
        ".tmp/../src",
        ".tmp/./x",
        "/.tmp/x",
        "C:/x/.tmp/y",
        ".tmp\\x",
        ".tmp/x*",
        ".tmp/x:stream",
        ".tmp/x.",
        ".tmp/x ",
        ".tmp//x",
    ],
)
def test_entry_paths_outside_the_strict_scratch_shape_are_refused(path: str) -> None:
    with pytest.raises(ManifestError):
        entry_components(path)


def test_an_ordinary_nested_entry_path_is_admitted() -> None:
    assert entry_components(".tmp/product-integrity/wt") == (".tmp", "product-integrity", "wt")


def _valid_manifest(workspace: Path) -> dict[str, Any]:
    _plain(workspace, "p")
    entry = _entry(workspace, "p", "plain_directory", "REMOVE_DISPOSABLE")
    return {
        "schema": retirement.RETIREMENT_MANIFEST_SCHEMA,
        "run_id": "run1",
        "receipts": ".receipts",
        "excluded_dir_names": EXCLUDED,
        "excluded_dir_markers": MARKERS,
        "entries": [entry],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(extra=1),
        lambda m: m.pop("run_id"),
        lambda m: m.update(schema="other/1"),
        lambda m: m.update(run_id="bad id"),
        lambda m: m.update(receipts=".tmp/receipts"),
        lambda m: m["entries"][0].update(extra=1),
        lambda m: m["entries"][0].update(kind="folder"),
        lambda m: m["entries"][0].update(disposition="DELETE"),
        lambda m: m["entries"][0].update(content_digest="abc"),
        lambda m: m["entries"][0].update(git={"head": "x"}),
        lambda m: m["entries"].append(dict(m["entries"][0])),
        lambda m: m["entries"].append({**m["entries"][0], "id": "other", "path": ".TMP/P"}),
        lambda m: m["entries"].append({**m["entries"][0], "id": "q", "path": ".tmp/P"}),
        lambda m: m["entries"][0].update(preservation={"receipt": "r", "sha256": "0" * 64}),
    ],
)
def test_a_manifest_that_is_not_exactly_closed_is_refused(workspace: Path, mutate: Any) -> None:
    manifest = _valid_manifest(workspace)
    mutate(manifest)
    with pytest.raises(ManifestError):
        validate_manifest(json.dumps(manifest).encode())


def test_the_command_line_refuses_a_manifest_whose_digest_differs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _context(workspace, [_valid_manifest(workspace)["entries"][0]])
    monkeypatch.setattr(retirement, "_repo_root", lambda: workspace)
    code = retirement.main(
        [
            "apply",
            "--manifest",
            str(workspace / "manifest.json"),
            "--manifest-sha256",
            "0" * 64,
            "--entry",
            "p",
        ]
    )
    assert code == 2
    assert (workspace / ".tmp" / "p").exists()


# ---------------------------------------------------------------------------------------------
# Registered worktrees
# ---------------------------------------------------------------------------------------------


def test_a_clean_worktree_is_removed_and_only_its_registration_is_dropped(
    workspace: Path, recorded_commands: list[list[str]]
) -> None:
    target = _worktree(workspace, "old")
    stale = _worktree(workspace, "stale")
    # A second registration whose directory is gone: a global prune would drop it too.
    for root, dirs, files in os.walk(stale, topdown=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(stale)
    refs_before = _refs(workspace)
    context = _context(workspace, [_entry(workspace, "old", "registered_worktree")])

    receipt = apply_entry(context, "old")

    assert receipt["state"] == "completed"
    assert not target.exists()
    assert not _is_registered(workspace, target)
    assert _is_registered(workspace, stale), "only the admitted registration may be dropped"
    assert _refs(workspace) == refs_before
    flat = [" ".join(call) for call in recorded_commands]
    assert not [c for c in flat if "--force" in c or " prune" in c], flat
    assert not [call for call in recorded_commands if call[0].lower().startswith("cmd")]


def test_links_inside_a_worktree_are_unlinked_and_their_targets_survive(
    workspace: Path, tmp_path: Path
) -> None:
    target = _worktree(workspace, "linked")
    canary = tmp_path / "canary"
    (canary / "deep").mkdir(parents=True)
    (canary / "deep" / "KEEP.txt").write_bytes(b"keep")
    internal = target / "ignored" / "real"
    internal.mkdir(parents=True)
    (internal / "f.txt").write_bytes(b"f")
    if not (
        _junction(target / "ignored" / "escape", canary)
        and _junction(target / "ignored" / "inner", internal)
    ):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    context = _context(workspace, [_entry(workspace, "linked", "registered_worktree")])

    apply_entry(context, "linked")

    assert not target.exists()
    assert (canary / "deep" / "KEEP.txt").read_bytes() == b"keep"


def test_a_locked_worktree_is_refused_before_any_effect(workspace: Path) -> None:
    target = _worktree(workspace, "locked")
    entry = _entry(workspace, "locked", "registered_worktree")
    _git("worktree", "lock", str(target), cwd=workspace)
    context = _context(workspace, [entry])

    with pytest.raises(Refused, match="locked"):
        apply_entry(context, "locked")

    assert target.exists() and _is_registered(workspace, target)
    assert not (workspace / ".receipts" / "locked.json").exists(), "the entry was renamed"


def test_a_git_refusal_after_the_quarantine_rename_restores_the_entry(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's lock refusal stands even when the admission did not see the lock."""
    target = _worktree(workspace, "racing")
    entry = _entry(workspace, "racing", "registered_worktree")
    context = _context(workspace, [entry])
    _git("worktree", "lock", str(target), cwd=workspace)
    real = retirement.registrations
    calls = {"n": 0}

    def unlocked_first(root: Path) -> dict[str, bool]:
        calls["n"] += 1
        seen = real(root)
        return {key: False for key in seen} if calls["n"] == 1 else seen

    monkeypatch.setattr(retirement, "registrations", unlocked_first)

    with pytest.raises(Refused, match="git refused"):
        apply_entry(context, "racing")

    assert target.exists() and _is_registered(workspace, target)
    receipt = json.loads((workspace / ".receipts" / "racing.json").read_bytes())
    assert receipt["state"] == "restored"
    with pytest.raises(Refused, match="restored"):
        apply_entry(context, "racing")


def test_new_content_after_the_manifest_is_refused(workspace: Path) -> None:
    target = _worktree(workspace, "drift")
    context = _context(workspace, [_entry(workspace, "drift", "registered_worktree")])
    (target / "ignored").mkdir()
    (target / "ignored" / "late.txt").write_bytes(b"late")

    with pytest.raises(Refused, match="content changed"):
        apply_entry(context, "drift")

    assert (target / "ignored" / "late.txt").exists()


def test_a_new_commit_after_the_manifest_is_refused(workspace: Path) -> None:
    target = _worktree(workspace, "moved")
    context = _context(workspace, [_entry(workspace, "moved", "registered_worktree")])
    _git("commit", "-q", "--allow-empty", "-m", "later", cwd=target)

    with pytest.raises(Refused, match="Git state changed"):
        apply_entry(context, "moved")

    assert target.exists()


def test_drift_seen_only_on_the_quarantined_tree_renames_it_back(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _worktree(workspace, "late")
    context = _context(workspace, [_entry(workspace, "late", "registered_worktree")])
    real = retirement.observe
    calls = {"n": 0}

    def second_differs(*args: Any) -> retirement.Observation:
        calls["n"] += 1
        seen = real(*args)
        if calls["n"] == 2:
            return retirement.Observation(seen.identity, "f" * 64, seen.git, seen.nested_git)
        return seen

    monkeypatch.setattr(retirement, "observe", second_differs)

    with pytest.raises(Refused, match="restored"):
        apply_entry(context, "late")

    assert target.exists() and _is_registered(workspace, target)
    assert not (workspace / ".tmp" / "late.retiring-run1").exists()


def test_a_dirty_worktree_is_admitted_only_in_its_preserved_state(workspace: Path) -> None:
    target = _worktree(workspace, "dirty")
    (target / "seed.txt").write_bytes(b"edited\n")
    (target / "new.txt").write_bytes(b"untracked\n")
    entry = _entry(workspace, "dirty", "registered_worktree", "PRESERVE_THEN_REMOVE")
    context = _context(workspace, [entry])

    apply_entry(context, "dirty")

    assert not target.exists() and not _is_registered(workspace, target)


@pytest.mark.parametrize("status", ["COPIED", "RESTORE_FAILED"])
def test_an_unverified_preservation_receipt_admits_nothing(workspace: Path, status: str) -> None:
    target = _worktree(workspace, "unproven")
    entry = _entry(
        workspace, "unproven", "registered_worktree", "PRESERVE_THEN_REMOVE", status=status
    )
    context = _context(workspace, [entry])

    with pytest.raises(Refused, match="no verified restore"):
        apply_entry(context, "unproven")

    assert target.exists()


def test_a_missing_or_altered_preservation_receipt_admits_nothing(workspace: Path) -> None:
    target = _worktree(workspace, "lost")
    entry = _entry(workspace, "lost", "registered_worktree", "PRESERVE_THEN_REMOVE")
    context = _context(workspace, [entry])
    receipt = workspace / ".receipts" / "lost.preservation.json"
    receipt.write_bytes(receipt.read_bytes() + b" ")
    with pytest.raises(Refused, match="digest differs"):
        apply_entry(context, "lost")
    receipt.unlink()
    with pytest.raises(Refused, match="missing"):
        apply_entry(context, "lost")
    assert target.exists()


@pytest.mark.skipif(os.name != "nt", reason="handle-based in-use refusal is Windows behaviour")
def test_an_open_file_inside_the_entry_keeps_it(workspace: Path) -> None:
    target = _worktree(workspace, "open")
    context = _context(workspace, [_entry(workspace, "open", "registered_worktree")])
    with open(target / "seed.txt", "rb"):
        with pytest.raises(Refused, match="in use"):
            apply_entry(context, "open")
    assert target.exists() and _is_registered(workspace, target)
    receipt = json.loads((workspace / ".receipts" / "open.json").read_bytes())
    assert receipt["state"] == "retained"
    apply_entry(context, "open")
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="handle-based in-use refusal is Windows behaviour")
def test_a_process_working_inside_the_entry_keeps_it(workspace: Path) -> None:
    """The process list cannot show a current directory; the rename refusal does."""
    target = _worktree(workspace, "busy")
    (target / "ignored").mkdir()
    context = _context(workspace, [_entry(workspace, "busy", "registered_worktree")])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=target / "ignored"
    )
    try:
        time.sleep(1)
        with pytest.raises(Refused, match="in use"):
            apply_entry(context, "busy")
    finally:
        child.kill()
        child.wait()
    assert target.exists() and _is_registered(workspace, target)


def test_a_process_naming_the_entry_keeps_it(workspace: Path) -> None:
    target = _worktree(workspace, "named")
    context = _context(workspace, [_entry(workspace, "named", "registered_worktree")])
    context.process_reader = lambda: [f"7\tpython.exe\tpython {workspace}\\.tmp\\named\\x.py"]
    with pytest.raises(Refused, match="running process"):
        apply_entry(context, "named")
    assert target.exists()


@pytest.mark.parametrize(("code", "stdout"), [(0, ""), (0, "\n  \n"), (1, "1\tx\ty\n")])
def test_an_unreadable_or_empty_process_list_blocks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    """The real reader, fed an empty or failed listing, must block rather than admit."""
    target = _worktree(workspace, "blind")
    context = _context(workspace, [_entry(workspace, "blind", "registered_worktree")])
    original = subprocess.run

    def listing(args: Any, *rest: Any, **kwargs: Any) -> Any:
        argv = [str(a) for a in args]
        if argv and argv[0] == "powershell":
            return subprocess.CompletedProcess(argv, code, stdout, "")
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", listing)
    if os.name != "nt":
        pytest.skip("the POSIX reader reads /proc directly")
    context.process_reader = retirement.read_processes
    with pytest.raises(Refused, match="process list"):
        apply_entry(context, "blind")
    assert target.exists()


def test_process_mentions_respect_name_boundaries() -> None:
    mentions = retirement._mentions
    assert mentions(["x\tpy\tpython B:\\ws\\.tmp\\l2r12\\a.py"], ".tmp/l2r12")
    assert mentions(['x\tpy\t"B:/ws/.tmp/l2r12"'], ".tmp/l2r12")
    assert mentions(["x\tpy\tcd /b/ws/.tmp/l2r12"], ".tmp/l2r12")
    assert not mentions(["x\tpy\tpython B:\\ws\\.tmp\\l2r12linux\\a.py"], ".tmp/l2r12")
    assert not mentions(["x\tpy\tpython B:\\ws\\.tmp\\l2r12-old"], ".tmp/l2r12")


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows process reader")
def test_the_process_reader_sees_a_non_ascii_command_line() -> None:
    token = "退役探針-" + str(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", token])
    try:
        time.sleep(1)
        lines = retirement.read_processes()
    finally:
        child.kill()
        child.wait()
    assert any(token in line for line in lines)


def test_an_interrupted_deletion_resumes_from_its_own_receipt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _worktree(workspace, "halted")
    context = _context(workspace, [_entry(workspace, "halted", "registered_worktree")])
    real = retirement.delete_tree

    def fail(root: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(retirement, "delete_tree", fail)
    with pytest.raises(Refused, match="residue kept"):
        apply_entry(context, "halted")
    quarantine = workspace / ".tmp" / "halted.retiring-run1"
    assert quarantine.exists() and not target.exists()
    assert not _is_registered(workspace, target)
    receipt = json.loads((workspace / ".receipts" / "halted.json").read_bytes())
    assert receipt["state"] == "deregistered"

    monkeypatch.setattr(retirement, "delete_tree", real)
    assert apply_entry(context, "halted")["state"] == "completed"
    assert not quarantine.exists()
    with pytest.raises(Refused, match="already retired"):
        apply_entry(context, "halted")


def test_a_resume_refuses_a_replaced_quarantine_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the directory the receipt admitted may be deleted on resume, never a newcomer."""
    _plain(workspace, "swapped")
    context = _context(
        workspace, [_entry(workspace, "swapped", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    real = retirement.delete_tree

    def fail(root: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(retirement, "delete_tree", fail)
    with pytest.raises(Refused, match="residue kept"):
        apply_entry(context, "swapped")
    quarantine = workspace / ".tmp" / "swapped.retiring-run1"
    os.rename(quarantine, workspace / ".tmp" / "admitted-original")
    quarantine.mkdir()
    (quarantine / "NEWCOMER.txt").write_bytes(b"not admitted")
    monkeypatch.setattr(retirement, "delete_tree", real)

    with pytest.raises(Refused, match="not the admitted one"):
        apply_entry(context, "swapped")
    assert (quarantine / "NEWCOMER.txt").exists()


class _Interrupted(BaseException):
    """Stands in for a process kill: no ``except`` clause of the tool may catch it."""


def _interrupt_second_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    real = retirement.observe
    calls = {"n": 0}

    def interrupted(*args: Any) -> retirement.Observation:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Interrupted()
        return real(*args)

    monkeypatch.setattr(retirement, "observe", interrupted)


def test_a_resume_from_quarantine_compares_again_before_deleting(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that arrived before the interrupted comparison is never deleted on resume."""
    target = _worktree(workspace, "interrupted")
    context = _context(workspace, [_entry(workspace, "interrupted", "registered_worktree")])
    _interrupt_second_observation(monkeypatch)
    with pytest.raises(_Interrupted):
        apply_entry(context, "interrupted")
    quarantine = workspace / ".tmp" / "interrupted.retiring-run1"
    receipt_path = workspace / ".receipts" / "interrupted.json"
    assert json.loads(receipt_path.read_bytes())["state"] == "quarantined"
    (quarantine / "late.txt").write_bytes(b"arrived after the first observation")
    monkeypatch.undo()

    with pytest.raises(Refused, match="restored"):
        apply_entry(context, "interrupted")

    assert (target / "late.txt").read_bytes() == b"arrived after the first observation"
    assert not quarantine.exists() and _is_registered(workspace, target)
    assert json.loads(receipt_path.read_bytes())["state"] == "restored"


def test_a_resume_from_quarantine_with_unchanged_content_completes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _worktree(workspace, "paused")
    context = _context(workspace, [_entry(workspace, "paused", "registered_worktree")])
    _interrupt_second_observation(monkeypatch)
    with pytest.raises(_Interrupted):
        apply_entry(context, "paused")
    monkeypatch.undo()

    receipt = apply_entry(context, "paused")

    assert receipt["state"] == "completed"
    states = [step["state"] for step in receipt["steps"]]
    assert states.index("verified") < states.index("deregistered") < states.index("completed")
    assert not target.exists() and not _is_registered(workspace, target)


def test_a_resume_with_both_paths_present_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _plain(workspace, "twice")
    context = _context(
        workspace, [_entry(workspace, "twice", "plain_directory", "REMOVE_DISPOSABLE")]
    )

    def fail(root: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(retirement, "delete_tree", fail)
    with pytest.raises(Refused, match="residue kept"):
        apply_entry(context, "twice")
    monkeypatch.undo()
    target.mkdir()
    (target / "NEW.txt").write_bytes(b"recreated")

    with pytest.raises(Refused, match="both the original and the quarantine path exist"):
        apply_entry(context, "twice")
    assert (target / "NEW.txt").exists()
    assert (workspace / ".tmp" / "twice.retiring-run1" / "out" / "result.txt").exists()


def test_a_receipt_from_another_manifest_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plain(workspace, "foreign")
    entry = _entry(workspace, "foreign", "plain_directory", "REMOVE_DISPOSABLE")

    def fail(root: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(retirement, "delete_tree", fail)
    with pytest.raises(Refused, match="residue kept"):
        apply_entry(_context(workspace, [entry], run_id="run1"), "foreign")
    monkeypatch.undo()

    with pytest.raises(Refused, match="another manifest"):
        apply_entry(_context(workspace, [entry], run_id="run2"), "foreign")
    assert (workspace / ".tmp" / "foreign.retiring-run1" / "out" / "result.txt").exists()


@pytest.mark.parametrize("bound", ["entry_id", "content_digest"])
def test_a_preservation_receipt_of_another_entry_or_content_admits_nothing(
    workspace: Path, bound: str
) -> None:
    target = _worktree(workspace, "borrowed")
    entry = _entry(workspace, "borrowed", "registered_worktree", "PRESERVE_THEN_REMOVE")
    other = {"entry_id": "someone-else", "content_digest": "0" * 64}
    receipt = {
        "schema": retirement.PRESERVATION_RECEIPT_SCHEMA,
        "entry_id": entry["id"],
        "content_digest": entry["content_digest"],
        "status": "RESTORE_VERIFIED",
        bound: other[bound],
    }
    raw = json.dumps(receipt).encode()
    (workspace / ".receipts" / "borrowed.preservation.json").write_bytes(raw)
    entry["preservation"]["sha256"] = hashlib.sha256(raw).hexdigest()
    context = _context(workspace, [entry])

    with pytest.raises(Refused, match="another entry or content"):
        apply_entry(context, "borrowed")
    assert target.exists() and _is_registered(workspace, target)


def test_a_directory_replaced_after_the_manifest_is_refused_by_identity(workspace: Path) -> None:
    """Same names, sizes and times in a different directory: only the identity tells them apart."""
    target = _plain(workspace, "lookalike")
    context = _context(
        workspace, [_entry(workspace, "lookalike", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    kept = workspace / ".tmp" / "lookalike-original"
    os.rename(target, kept)
    shutil.copytree(kept, target)

    with pytest.raises(Refused, match="directory identity differs from the manifest"):
        apply_entry(context, "lookalike")
    assert (target / "out" / "result.txt").exists() and kept.exists()
    # refused at admission: never renamed, so no receipt was written
    assert not (workspace / ".receipts" / "lookalike.json").exists()


def test_a_quarantined_identity_mismatch_renames_it_back(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _worktree(workspace, "swapped-id")
    context = _context(workspace, [_entry(workspace, "swapped-id", "registered_worktree")])
    real = retirement.observe
    calls = {"n": 0}

    def other_identity(*args: Any) -> retirement.Observation:
        calls["n"] += 1
        seen = real(*args)
        if calls["n"] == 2:
            return retirement.Observation(
                {"device": 0, "inode": 0}, seen.content_digest, seen.git, seen.nested_git
            )
        return seen

    monkeypatch.setattr(retirement, "observe", other_identity)

    with pytest.raises(Refused, match="quarantined directory identity differs"):
        apply_entry(context, "swapped-id")
    assert target.exists() and _is_registered(workspace, target)


def test_a_registered_worktree_declared_as_a_plain_directory_is_refused(workspace: Path) -> None:
    target = _worktree(workspace, "undeclared")
    entry = _entry(workspace, "undeclared", "plain_directory", "REMOVE_DISPOSABLE")
    context = _context(workspace, [entry])

    with pytest.raises(Refused, match="registered worktree but not declared"):
        apply_entry(context, "undeclared")
    assert target.exists() and _is_registered(workspace, target)


def test_a_plain_directory_with_a_git_pointer_file_is_refused(workspace: Path) -> None:
    target = _plain(workspace, "pointer")
    entry = _entry(workspace, "pointer", "plain_directory", "REMOVE_DISPOSABLE")
    (target / ".git").write_bytes(b"gitdir: elsewhere\n")
    context = _context(workspace, [entry])

    with pytest.raises(Refused, match="plain directory holds a .git entry"):
        apply_entry(context, "pointer")
    assert (target / ".git").exists()


def test_an_unproven_link_inside_an_excluded_directory_stops_the_deletion(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """observe() does not look inside excluded directories; deletion enforces invariant 4 there."""
    if os.name != "nt":
        pytest.skip("reparse tags are Windows metadata")
    outside = tmp_path / "outside-excluded"
    outside.mkdir()
    (outside / "KEEP.txt").write_bytes(b"keep")
    target = _plain(workspace, "hidden-link")
    (target / "node_modules").mkdir()
    if not _junction(target / "node_modules" / "pkg", outside):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    entry = _entry(workspace, "hidden-link", "plain_directory", "REMOVE_DISPOSABLE")
    context = _context(workspace, [entry])
    monkeypatch.setattr(retirement, "KNOWN_LINK_TAGS", frozenset())

    with pytest.raises(Refused, match="residue kept"):
        apply_entry(context, "hidden-link")

    assert (outside / "KEEP.txt").read_bytes() == b"keep"
    receipt = json.loads((workspace / ".receipts" / "hidden-link.json").read_bytes())
    assert receipt["state"] == "verified"
    assert "unproven kind" in receipt["steps"][-1]["deletion_error"]


def test_the_receipt_records_the_process_check(workspace: Path) -> None:
    _plain(workspace, "observed")
    context = _context(
        workspace, [_entry(workspace, "observed", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    receipt = apply_entry(context, "observed")
    admitted = next(step for step in receipt["steps"] if step["state"] == "admitted")
    assert admitted["processes_read"] == len(_quiet_processes())
    assert admitted["processes_naming_entry"] == 0


# ---------------------------------------------------------------------------------------------
# Clones, plain directories and containers
# ---------------------------------------------------------------------------------------------


def test_an_independent_clone_is_removed_without_touching_the_root_refs(
    workspace: Path, recorded_commands: list[list[str]]
) -> None:
    clone = workspace / ".tmp" / "clone"
    _git("clone", "-q", str(workspace), str(clone), cwd=workspace)
    _git("commit", "-q", "--allow-empty", "-m", "only-in-clone", cwd=clone)
    refs_before = _refs(workspace)
    entry = _entry(workspace, "clone", "independent_clone", "PRESERVE_THEN_REMOVE")
    context = _context(workspace, [entry])

    apply_entry(context, "clone")

    assert not clone.exists()
    assert _refs(workspace) == refs_before
    assert not [call for call in recorded_commands if "fetch" in call]


def test_a_clone_whose_refs_moved_is_refused(workspace: Path) -> None:
    clone = workspace / ".tmp" / "clone2"
    _git("clone", "-q", str(workspace), str(clone), cwd=workspace)
    context = _context(workspace, [_entry(workspace, "clone2", "independent_clone")])
    _git("branch", "extra", cwd=clone)
    with pytest.raises(Refused, match="Git state changed"):
        apply_entry(context, "clone2")
    assert clone.exists()


def test_a_disposable_plain_directory_is_removed(workspace: Path) -> None:
    target = _plain(workspace, "scratch")
    (target / "node_modules" / "pkg").mkdir(parents=True)
    context = _context(
        workspace, [_entry(workspace, "scratch", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    apply_entry(context, "scratch")
    assert not target.exists()


def test_a_plain_directory_holding_a_repository_is_refused(workspace: Path) -> None:
    target = _plain(workspace, "holder")
    _git("init", "-q", str(target / "out" / "inner"), cwd=workspace)
    context = _context(
        workspace, [_entry(workspace, "holder", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    with pytest.raises(Refused, match="nested repository"):
        apply_entry(context, "holder")
    assert target.exists()


def test_a_container_of_a_registered_worktree_is_refused(workspace: Path) -> None:
    box = workspace / ".tmp" / "box"
    box.mkdir()
    _git("worktree", "add", "-q", "--detach", str(box / "inner"), cwd=workspace)
    entry = _entry(workspace, "box", "plain_directory", "PRESERVE_THEN_REMOVE")
    context = _context(workspace, [entry])
    with pytest.raises(Refused, match="registered worktree lies inside"):
        apply_entry(context, "box")
    assert (box / "inner" / "seed.txt").exists()


def test_a_nested_entry_beneath_a_linked_ancestor_is_refused(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "victim").mkdir(parents=True)
    (outside / "victim" / "KEEP.txt").write_bytes(b"keep")
    if not _junction(workspace / ".tmp" / "alias", outside):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    entry = _entry(workspace, "victim", "plain_directory", "KEEP_ACTIVE", path=".tmp/alias/victim")
    entry.update(
        disposition="REMOVE_DISPOSABLE",
        identity={"device": 0, "inode": 0},
        content_digest="0" * 64,
    )
    context = _context(workspace, [entry])
    with pytest.raises(Refused, match="ancestor alias"):
        apply_entry(context, "victim")
    assert (outside / "victim" / "KEEP.txt").exists()


def test_an_entry_that_is_itself_a_link_is_refused(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "KEEP.txt").write_bytes(b"keep")
    if not _junction(workspace / ".tmp" / "jl", outside):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    entry = _entry(workspace, "jl", "plain_directory", "KEEP_ACTIVE")
    entry.update(
        disposition="REMOVE_DISPOSABLE",
        identity={"device": 0, "inode": 0},
        content_digest="0" * 64,
    )
    context = _context(workspace, [entry])
    with pytest.raises(Refused, match="alias|ordinary"):
        apply_entry(context, "jl")
    assert (outside / "KEEP.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="read-only attributes and hard links are NTFS here")
def test_a_read_only_hard_link_is_deleted_without_changing_the_shared_file(
    workspace: Path, tmp_path: Path
) -> None:
    """Research P4: Git clears this attribute; the walker must not, on any link."""
    store = tmp_path / "store.bin"
    store.write_bytes(b"shared")
    target = _plain(workspace, "linked-store")
    os.link(store, target / "out" / "store.bin")
    os.chmod(store, stat.S_IREAD)
    ro_dir = target / "out" / "ro"
    ro_dir.mkdir()
    (ro_dir / "solo.txt").write_bytes(b"solo")
    os.chmod(ro_dir / "solo.txt", stat.S_IREAD)
    os.chmod(ro_dir, stat.S_IREAD)
    try:
        context = _context(
            workspace, [_entry(workspace, "linked-store", "plain_directory", "REMOVE_DISPOSABLE")]
        )
        apply_entry(context, "linked-store")
        assert not target.exists()
        assert store.read_bytes() == b"shared"
        assert getattr(os.stat(store), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
        assert os.stat(store).st_nlink == 1
    finally:
        os.chmod(store, stat.S_IWRITE)


def _wsl_path(path: Path) -> str | None:
    if os.name != "nt" or not shutil.which("wsl"):
        return None
    done = subprocess.run(
        ["wsl", "wslpath", "-a", str(path).replace("\\", "/")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def test_wsl_symlinks_are_unlinked_and_their_targets_survive(
    workspace: Path, tmp_path: Path
) -> None:
    """WSL symlinks carry a reparse tag Windows never follows and os.readlink cannot read.

    A link to a directory also carries the directory attribute, which os.unlink refuses; the
    walker must still remove the link object and never its target.
    """
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "KEEP.txt").write_bytes(b"keep")
    target = _plain(workspace, "wsl-links")
    inside = _wsl_path(target / "out")
    outside = _wsl_path(canary)
    if inside is None or outside is None:
        pytest.skip("WSL is not available to create WSL symlinks")
    # A relative link to an existing directory is the shape WSL marks with the directory
    # attribute; it escapes the entry to the canary.
    relative = os.path.relpath(canary, target / "out").replace("\\", "/")
    script = (
        f"cd '{inside}' && ln -s '{relative}' dirlink && ln -s '{outside}/KEEP.txt' filelink "
        "&& ln -s /nonexistent/x dangling"
    )
    made = subprocess.run(["wsl", "bash", "-c", script], capture_output=True, check=False)
    if made.returncode != 0:
        pytest.skip("WSL could not create symlinks on this filesystem")
    names = ("dirlink", "filelink", "dangling")
    metadata = {name: os.lstat(target / "out" / name) for name in names}
    tags = {name: getattr(md, "st_reparse_tag", 0) for name, md in metadata.items()}
    assert set(tags.values()) == {0xA000001D}, tags
    assert getattr(metadata["dirlink"], "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY
    context = _context(
        workspace, [_entry(workspace, "wsl-links", "plain_directory", "REMOVE_DISPOSABLE")]
    )

    apply_entry(context, "wsl-links")

    assert not target.exists()
    assert (canary / "KEEP.txt").read_bytes() == b"keep"


def test_a_reparse_point_of_an_unproven_kind_is_refused(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _plain(workspace, "unknown-tag")
    if not _junction(target / "out" / "link", outside):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    entry = _entry(workspace, "unknown-tag", "plain_directory", "REMOVE_DISPOSABLE")
    context = _context(workspace, [entry])
    monkeypatch.setattr(retirement, "KNOWN_LINK_TAGS", frozenset())
    if os.name != "nt":
        pytest.skip("reparse tags are Windows metadata")

    with pytest.raises(Refused, match="unproven kind"):
        apply_entry(context, "unknown-tag")
    assert target.exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises the extended-length spelling")
def test_a_path_beyond_max_path_is_deleted(workspace: Path) -> None:
    target = _plain(workspace, "deep")
    current = "\\\\?\\" + str(target / "out")
    for index in range(30):
        current += "\\" + f"segment-{index:02d}-abcdefgh"
        os.mkdir(current)
    with open(current + "\\leaf.txt", "wb") as handle:
        handle.write(b"leaf")
    assert len(current) > 300
    context = _context(
        workspace, [_entry(workspace, "deep", "plain_directory", "REMOVE_DISPOSABLE")]
    )
    apply_entry(context, "deep")
    assert not target.exists()


# ---------------------------------------------------------------------------------------------
# Preview and verify
# ---------------------------------------------------------------------------------------------


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    rows = []
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(directory, name)
            metadata = os.lstat(path)
            rows.append((os.path.relpath(path, root), metadata.st_size, metadata.st_mtime_ns))
    return sorted(rows)


def test_preview_has_no_effects_and_reports_each_entry(workspace: Path) -> None:
    _worktree(workspace, "a")
    _plain(workspace, "b")
    _plain(workspace, "keep")
    entries = [
        _entry(workspace, "a", "registered_worktree"),
        _entry(workspace, "b", "plain_directory", "REMOVE_DISPOSABLE"),
        _entry(workspace, "keep", "plain_directory", "KEEP_RECOVERY"),
    ]
    context = _context(workspace, entries)
    (workspace / ".tmp" / "b" / "late.txt").write_bytes(b"late")
    before = _snapshot(workspace / ".tmp")
    registered = _registered(workspace)

    rows = {row["id"]: row for row in preview(context)}

    assert rows["a"]["result"] == "would-remove"
    assert rows["b"]["result"] == "refused"
    assert rows["keep"]["result"] == "keep"
    assert _snapshot(workspace / ".tmp") == before
    assert _registered(workspace) == registered
    assert not list((workspace / ".receipts").glob("*.json.partial"))


def test_verify_is_repeatable_and_reports_violations(workspace: Path) -> None:
    _worktree(workspace, "gone")
    _plain(workspace, "kept")
    _plain(workspace, "later")
    entries = [
        _entry(workspace, "gone", "registered_worktree"),
        _entry(workspace, "kept", "plain_directory", "KEEP_ACTIVE"),
        _entry(workspace, "later", "plain_directory", "REMOVE_DISPOSABLE"),
    ]
    context = _context(workspace, entries)
    apply_entry(context, "gone")

    first = verify(context)
    assert first == verify(context)
    results = {row["id"]: row["result"] for row in first}
    assert results == {"gone": "ok", "kept": "ok", "later": "pending"}

    retirement.delete_tree(workspace / ".tmp" / "kept")
    retirement.delete_tree(workspace / ".tmp" / "later")
    results = {row["id"]: row["result"] for row in verify(context)}
    assert results["kept"].startswith("VIOLATION")
    assert results["later"].startswith("VIOLATION")
