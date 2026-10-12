"""workspace link-hazard guard regressions.

The hazard: a directory link inside a git worktree is followed by ``git worktree remove``, which
deletes the link target's contents.  When the link path is gitignored the plain, unforced command
does it silently.  These tests build real links -- not mocks -- because the entire defect class
comes from a real link type behaving unlike the one everybody checks for.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.workspace_link_guard import (
    LinkFinding,
    deletable_unit,
    find_links,
    is_reparse_point,
    main,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_directory_link(link: Path, target: Path) -> bool:
    """A junction on Windows, a symlink elsewhere.  False when the platform refuses.

    Windows junctions need no privilege; symlinks need Developer Mode or elevation, which this
    project's environment does not have.  Junction creation must go through ``cmd`` directly --
    invoked from a POSIX emulation layer it fails on path translation.
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0 and link.exists()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


def _worktree_with_link(tmp_path: Path, target: Path) -> Path:
    """A worktree-shaped directory under .tmp/, holding one link to target.

    It must live under ``.tmp/<entry>`` for the production containment rule to apply: an
    unregistered directory sitting directly in the repository root is not a unit anyone deletes
    recursively, and the guard deliberately does not treat it as one.
    """
    # IMPORTANT: local pytest temp may be inside a real worktree. Give the fixture its own Git
    # root, or Git discovers the enclosing checkout and misclassifies this fixture's junction.
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    worktree = tmp_path / ".tmp" / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "module.py").write_text("x", encoding="utf-8")
    if not _make_directory_link(worktree / ".venv", target):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    return worktree


def test_a_junction_is_a_reparse_point_that_os_path_islink_does_not_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason this module exists, asserted directly rather than assumed."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    if not _make_directory_link(link, target):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    assert is_reparse_point(link)
    monkeypatch.chdir(tmp_path)
    assert is_reparse_point(Path("link")), "relative paths must preserve link classification"
    if os.name == "nt":
        assert not os.path.islink(link), (
            "a Windows junction must remain invisible to os.path.islink; if this ever changes the "
            "guard is still correct, but the reason for it has moved"
        )


def test_link_escaping_its_worktree_is_flagged(tmp_path: Path) -> None:
    target = tmp_path / "shared_venv"
    target.mkdir()
    (target / "payload.txt").write_text("would be destroyed", encoding="utf-8")
    worktree = _worktree_with_link(tmp_path, target)

    findings = find_links(worktree, repo_root=tmp_path)
    escaping = [f for f in findings if f.escapes]
    assert len(escaping) == 1
    assert escaping[0].path.name == ".venv"
    assert escaping[0].target == target.resolve()


def test_link_resolving_back_inside_the_worktree_is_not_flagged(tmp_path: Path) -> None:
    """pnpm fills node_modules with these; a worktree of this project holds 265."""
    worktree = tmp_path / ".tmp" / "wt"
    store = worktree / "node_modules" / ".pnpm" / "pkg"
    store.mkdir(parents=True)
    (store / "index.js").write_text("x", encoding="utf-8")
    if not _make_directory_link(worktree / "node_modules" / "pkg", store):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    findings = find_links(worktree, repo_root=tmp_path)
    assert findings, "the link must be detected"
    assert not [f for f in findings if f.escapes], "an internal link must not be flagged"


def test_a_dangling_link_is_reported_but_does_not_fail(tmp_path: Path) -> None:
    """This repository carries 236 of them under frontend/node_modules after a rename."""
    target = tmp_path / "gone"
    target.mkdir()
    worktree = _worktree_with_link(tmp_path, target)
    target.rmdir()

    findings = find_links(worktree, repo_root=tmp_path)
    assert len(findings) == 1
    assert findings[0].leaves_container
    assert findings[0].dangling
    assert not findings[0].escapes, "nothing lives behind a dangling link, so nothing can be lost"
    assert findings[0].classification == "dangling"


def test_walk_terminates_on_a_link_pointing_at_its_own_root(tmp_path: Path) -> None:
    """The loop that turned a 6.76 GB measurement into an apparent 67 GB."""
    worktree = tmp_path / ".tmp" / "wt"
    (worktree / "sub").mkdir(parents=True)
    if not _make_directory_link(worktree / "sub" / "self", worktree):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    findings = find_links(worktree, repo_root=tmp_path)
    assert [f for f in findings if f.path.name == "self"]


def test_a_queued_directory_replaced_by_a_junction_is_never_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    queued = root / "queued"
    queued.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(root)],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / "canary.txt").write_text("must not be traversed", encoding="utf-8")
    original = os.scandir
    descended: list[Path] = []

    class ReplacedAfterEnumeration:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.entry = entry
            self.path = entry.path
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            return self.entry.stat(follow_symlinks=follow_symlinks)

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            result = self.entry.is_dir(follow_symlinks=follow_symlinks)
            queued.rmdir()
            if not _make_directory_link(queued, target):
                pytest.skip("directory links unavailable")
            return result

    def scandir(path: os.PathLike[str] | str) -> object:
        canonical = Path(os.fspath(path).removeprefix("\\\\?\\"))
        descended.append(canonical)
        entries = list(original(path))
        if canonical == root:
            return iter(
                ReplacedAfterEnumeration(entry) if entry.name == "queued" else entry
                for entry in entries
            )
        return iter(entries)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        findings = find_links(root, repo_root=root, strict=True)
        assert len(findings) == 1 and findings[0].path == queued
        assert findings[0].escapes
        assert queued not in descended and target not in descended
    finally:
        if is_reparse_point(queued):
            if os.name == "nt":
                os.rmdir(queued)
            else:
                queued.unlink()
    assert (target / "canary.txt").read_text(encoding="utf-8") == "must not be traversed"


def test_a_long_windows_directory_is_censused_without_following_its_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows wildcard enumeration boundary")
    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(root)],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "target"
    target.mkdir()
    canary = target / "canary.txt"
    canary.write_text("keep the target", encoding="utf-8")
    deep = root
    assert len(str(deep)) < 258
    while len(str(deep)) < 259:
        distance = 259 - len(str(deep))
        component = min(30, distance - 1)
        if distance - component - 1 == 1:
            component -= 1
        deep /= "d" * component
    assert len(str(deep)) == 259
    extended = "\\\\?\\" + str(deep)
    os.makedirs(extended)
    link = deep / "junction"
    extended_link = "\\\\?\\" + str(link)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", extended_link, str(target)],
        check=True,
        capture_output=True,
    )
    original = os.scandir
    entered: list[str] = []

    def observe(path: os.PathLike[str] | str) -> object:
        entered.append(os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    try:
        findings = find_links(root, repo_root=root, strict=True)
        assert len(findings) == 1
        assert findings[0].path == link
        assert findings[0].target == target.resolve()
        assert findings[0].escapes
        assert str(link) not in entered and extended_link not in entered
    finally:
        os.rmdir(extended_link)
    assert canary.read_text(encoding="utf-8") == "keep the target"


def test_check_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.workspace_link_guard._repo_root", lambda: tmp_path)
    target = tmp_path / "shared"
    target.mkdir()
    (target / "payload.txt").write_text("x", encoding="utf-8")
    worktree = _worktree_with_link(tmp_path, target)

    assert main(["check", str(worktree)]) == 1
    assert "workspace link guard: FAIL" in capsys.readouterr().err

    clean = tmp_path / ".tmp" / "clean"
    (clean / "src").mkdir(parents=True)
    assert main(["check", str(clean)]) == 0


def test_check_failure_names_the_path_and_a_remedy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.workspace_link_guard._repo_root", lambda: tmp_path)
    target = tmp_path / "shared"
    target.mkdir()
    (target / "payload.txt").write_text("x", encoding="utf-8")
    worktree = _worktree_with_link(tmp_path, target)

    main(["check", str(worktree)])
    err = capsys.readouterr().err
    assert ".venv" in err
    assert "remove-worktree" in err, "a guard that does not say what to do next is a wall"


def test_deletable_unit_is_the_enclosing_worktree_then_the_scratch_entry() -> None:
    repo = Path("/repo").resolve()
    worktree = (repo / ".tmp" / "wt").resolve()

    inside = deletable_unit((worktree / "a" / ".venv").resolve(), repo, [repo, worktree])
    assert inside == worktree

    scratch = deletable_unit((repo / ".tmp" / "loose" / "deep" / "link").resolve(), repo, [repo])
    assert scratch == (repo / ".tmp" / "loose").resolve()

    plain = deletable_unit((repo / "frontend" / "link").resolve(), repo, [repo])
    assert plain == repo


def test_escapes_is_derived_not_stored() -> None:
    """A finding must not be able to disagree with its own paths."""
    finding = LinkFinding(
        path=Path("/repo/.tmp/wt/.venv"),
        target=Path("/repo/.venv"),
        container=Path("/repo/.tmp/wt"),
    )
    assert finding.leaves_container
    assert finding.dangling  # neither path exists in this synthetic case
    assert not finding.escapes


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length target regression")
def test_a_long_live_target_cannot_be_mistaken_for_a_dangling_junction(tmp_path: Path) -> None:
    from scripts.gate_link_receipt import _topology

    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(root)],
        check=True,
        capture_output=True,
    )
    target = tmp_path / "outside"
    while len(str(target)) < 310:
        target /= "d" * 30
    native_target = "\\\\?\\" + str(target)
    os.makedirs(native_target)
    canary = Path(native_target) / "canary.txt"
    canary.write_text("must survive the census", encoding="utf-8")
    link = root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), native_target],
        check=True,
        capture_output=True,
    )
    try:
        findings = find_links(root, repo_root=root, strict=True)
        assert len(findings) == 1
        assert findings[0].path == link and findings[0].target == target
        assert not findings[0].dangling and findings[0].escapes
        with pytest.raises(ValueError, match="escaping live link"):
            _topology(root, findings)
    finally:
        os.rmdir(link)
    assert canary.read_text(encoding="utf-8") == "must survive the census"


@pytest.mark.skipif(os.name != "nt", reason="Windows native containment spellings")
@pytest.mark.parametrize(
    "native,expected",
    [
        ("\\\\?\\C:\\fixture", "C:\\fixture"),
        ("\\\\?\\UNC\\fixture.invalid\\share\\fixture", "\\\\fixture.invalid\\share\\fixture"),
        (
            "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\fixture",
            "\\\\?\\Volume{00000000-0000-0000-0000-000000000001}\\fixture",
        ),
    ],
)
def test_native_io_spelling_keeps_an_absolute_containment_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: str, expected: str
) -> None:
    from scripts.workspace_link_guard import _real_path

    monkeypatch.setattr(os.path, "realpath", lambda path: native)
    target = _real_path(tmp_path / "fixture")
    assert target == Path(expected) and os.path.isabs(str(target))


def test_the_repository_holds_no_escaping_link() -> None:
    """The invariant the Full Gate stage enforces."""
    from scripts.gate_link_receipt import authentic_current_receipt

    if authentic_current_receipt(ROOT):
        pytest.skip("fresh authenticated Full Gate stage-1 census covers this exact repository")
    escaping = [f for f in find_links(ROOT, repo_root=ROOT) if f.escapes]
    assert escaping == [], (
        "an escaping link is present; a recursive delete of its container would destroy its "
        f"target: {[str(f.path) for f in escaping]}"
    )


@pytest.mark.parametrize("error_type", [PermissionError, ValueError])
def test_unavailable_receipt_does_not_replace_the_original_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    from scripts import gate_link_receipt as receipt

    root = tmp_path / "root"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--template=", str(root)],
        check=True,
        capture_output=True,
    )
    path = root / ".tmp/gate-link-receipts/owned-run/receipt.json"
    monkeypatch.setenv(receipt.RECEIPT_ENV, str(path))
    issued: list[Path] = []

    def unavailable(repo: Path, findings: list[LinkFinding]) -> None:
        issued.append(repo)
        raise error_type("controlled unavailable proof")

    monkeypatch.setattr(receipt, "issue", unavailable)
    assert main(["check", str(root)]) == 0
    assert issued == [root]
    assert "fresh gate receipt issued" not in capsys.readouterr().out
    assert not path.exists() and not receipt.authentic_current_receipt(root)


def test_uninspectable_census_still_fails_before_receipt_issuance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import gate_link_receipt as receipt

    issued: list[Path] = []
    monkeypatch.setenv(receipt.RECEIPT_ENV, str(tmp_path / "unused.json"))
    monkeypatch.setattr(receipt, "issue", lambda root, findings: issued.append(root))

    def unavailable(path: os.PathLike[str] | str) -> object:
        raise PermissionError("controlled unreadable census")

    monkeypatch.setattr(os, "scandir", unavailable)
    assert main(["check", str(tmp_path)]) == 1
    assert not issued


def _seed_repo_with_worktree(base: Path, gitignore: str) -> tuple[Path, Path, Path]:
    """A real git repo, a real registered worktree, and a canary the worktree links to."""
    canary = base / "canary"
    (canary / "deep").mkdir(parents=True)
    (canary / "CANARY.txt").write_text("do-not-delete", encoding="utf-8")
    (canary / "deep" / "DEEP.txt").write_text("nested", encoding="utf-8")

    repo = base / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(base / "gitconfig"), "GIT_CONFIG_SYSTEM": ""}

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
        )

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    worktree = base / ".tmp" / "wt"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "--detach", str(worktree))
    return repo, worktree, canary


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows follows ignored directory junctions")
def test_bare_git_worktree_remove_destroys_the_link_target(tmp_path: Path) -> None:
    """The control arm. If this ever stops failing, upstream fixed it and the guard can relax."""
    repo, worktree, canary = _seed_repo_with_worktree(tmp_path, ".venv/\n")
    if not _make_directory_link(worktree / ".venv", canary):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    subprocess.run(
        ["git", "worktree", "remove", str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert not (canary / "CANARY.txt").exists(), (
        "expected the documented destruction; the plain unforced command follows a gitignored "
        "junction and deletes its target"
    )


def test_remove_worktree_mode_preserves_the_link_target(tmp_path: Path) -> None:
    """The same configuration, through the guard. AC--04."""
    repo, worktree, canary = _seed_repo_with_worktree(tmp_path, ".venv/\n")
    if not _make_directory_link(worktree / ".venv", canary):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    findings = find_links(worktree, repo_root=repo)
    assert [f for f in findings if f.escapes], "fixture must reproduce the hazard"

    for finding in (f for f in findings if f.escapes):
        from scripts.workspace_link_guard import detach

        assert detach(finding.path, finding.target)

    subprocess.run(
        ["git", "worktree", "remove", str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (canary / "CANARY.txt").exists(), "detaching first must leave the target alone"
    assert (canary / "deep" / "DEEP.txt").exists()
    assert not worktree.exists(), "the worktree itself must still be gone"


# --------------------------------------------------------------------------------------------
# teardown completion.
#
#  shipped `remove-worktree` as the sanctioned teardown path and it did not complete: it
# left both M23 worktrees as deregistered husks.  The cause was probed rather than assumed, and
# the first hypothesis -- that ignored build output blocks removal -- was refuted.  What actually
# defeats git is an *internal* junction, which is how pnpm links every package.
# --------------------------------------------------------------------------------------------


def _internal_junction(worktree: Path) -> bool:
    """The pnpm layout: node_modules/pkg is a junction into node_modules/.pnpm/... ."""
    real = worktree / "node_modules" / ".pnpm" / "pkg@1.0.0" / "node_modules" / "pkg"
    real.mkdir(parents=True)
    (real / "index.js").write_text("module.exports = 1", encoding="utf-8")
    return _make_directory_link(worktree / "node_modules" / "pkg", real)


def _ignored_build_output(worktree: Path) -> None:
    for relative in ("node_modules/plain/lib", ".venv/Scripts"):
        (worktree / relative).mkdir(parents=True)
        (worktree / relative / "f.txt").write_text("build output", encoding="utf-8")


def _seed_repo_with_worktree_inside(base: Path) -> tuple[Path, Path]:
    """The production layout: the worktree sits inside the repository, under an ignored .tmp/."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(base / "gitconfig"), "GIT_CONFIG_SYSTEM": ""}

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
        )

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    # None of these names may be a contract path: `contract_inventory.py` tokenises file text, so
    # naming one here would enrol this test module in that contract and stale the artifact.
    for name in ("seed.txt", "README.md", "LICENSE", "NOTICE"):
        (repo / name).write_text(name, encoding="utf-8")
    (repo / ".gitignore").write_text(".tmp/\nnode_modules/\n.venv/\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    worktree = repo / ".tmp" / "wt"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "--detach", str(worktree))
    return repo, worktree


def _use_fixture_repo(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Point the module's repository root at the fixture instead of this checkout."""
    import scripts.workspace_link_guard as guard

    monkeypatch.setattr(guard, "_repo_root", lambda: repo)


def _registered(repo: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        os.path.normcase(str(Path(line[len("worktree ") :].strip()).resolve()))
        for line in completed.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _is_registered(repo: Path, worktree: Path) -> bool:
    return os.path.normcase(str(worktree.resolve())) in _registered(repo)


def _git_remove(repo: Path, worktree: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "worktree", "remove", *flags, str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("flags", [(), ("--force",)])
@pytest.mark.skipif(os.name != "nt", reason="Git for Windows leaves an internal-junction husk")
def test_git_worktree_remove_is_defeated_by_an_internal_junction(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    """The control arm for, and the reason the tool cannot simply delegate.

    ``--force`` is parametrised deliberately: it overrides git's policy checks, not ``rmdir``, so
    it makes no difference here.  If either arm ever starts passing, upstream fixed the deletion
    and the completion step in ``_remove_worktree`` can be reconsidered.
    """
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n.venv/\n")
    if not _internal_junction(worktree):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    completed = _git_remove(repo, worktree, *flags)

    assert completed.returncode != 0, "expected the documented failure"
    assert worktree.exists(), "the husk is the defect: git gives up with the directory in place"
    assert not _is_registered(repo, worktree), (
        "and it deregisters the worktree anyway, which is what makes a retry impossible"
    )


def test_ignored_build_output_alone_does_not_block_removal(tmp_path: Path) -> None:
    """The refuted hypothesis, asserted so it cannot quietly return.

    's first plan blamed ignored build output and proposed forcing past it. Both halves
    were wrong, and this arm is what says so.
    """
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n.venv/\n")
    _ignored_build_output(worktree)

    completed = _git_remove(repo, worktree)

    assert completed.returncode == 0, "ignored content is not the blocker"
    assert not worktree.exists()


def test_remove_worktree_finishes_what_git_could_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC--01, and AC--04's second half: no --force is needed."""
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    if not _internal_junction(worktree):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 0
    assert not worktree.exists()
    assert not _is_registered(repo, worktree)


def test_an_uncommitted_tracked_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC--02."""
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    (worktree / "seed.txt").write_text("edited and never committed", encoding="utf-8")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 1
    assert "uncommitted work" in capsys.readouterr().err
    assert worktree.exists() and _is_registered(repo, worktree)
    assert (worktree / "seed.txt").read_text(encoding="utf-8") == "edited and never committed"


def test_an_unreachable_detached_head_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC--03: a commit made on a detached HEAD exists nowhere else."""
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    (worktree / "new.txt").write_text("work that exists only here", encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "detached work")):
        subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, check=False)
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 1
    assert "unreachable detached HEAD" in capsys.readouterr().err
    assert worktree.exists()


def test_a_branch_worktree_is_never_treated_as_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction that stopped the reachability check from being vacuous.

    Phrased as "reachable from some ref", the check passes trivially for every branch worktree and
    asserts nothing.  Removing a branch worktree cannot lose commits, because git leaves the branch
    behind, so only a detached HEAD is ever at risk.
    """
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    subprocess.run(
        ["git", "checkout", "-q", "-b", "side"], cwd=worktree, capture_output=True, text=True
    )
    (worktree / "new.txt").write_text("committed on a branch", encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-q", "-m", "branch work")):
        subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True, check=False)
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 0
    assert not worktree.exists()
    survives = subprocess.run(
        ["git", "rev-parse", "--verify", "side"], cwd=repo, capture_output=True, text=True
    )
    assert survives.returncode == 0, (
        "the branch must outlive its worktree; that is why this is safe"
    )


def test_force_overrides_a_refusal_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC--04: an override has to be visible in a command log, not silent."""
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    (worktree / "seed.txt").write_text("edited and never committed", encoding="utf-8")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree), "--force"]) == 0
    out = capsys.readouterr().out
    assert "--force overriding refusal" in out and "uncommitted work" in out
    assert not worktree.exists()


@pytest.mark.skipif(os.name != "nt", reason="Fixture requires a real Git for Windows junction husk")
def test_a_real_husk_is_refused_rather_than_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC--05: nothing but a registered worktree is deleted, husks included.

    A ``--remnant`` mode was built and withdrawn.  Twice a rule meant to recognise "this used to
    be a worktree" admitted real directories, the second time an ordinary ``.venv``, so the
    capability was removed rather than guarded by a third predicate.  A husk is now inspected with
    ``check`` and deleted by hand.
    """
    repo, worktree = _seed_repo_with_worktree_inside(tmp_path)
    if not _internal_junction(worktree):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    assert _git_remove(repo, worktree).returncode != 0
    assert worktree.exists() and not _is_registered(repo, worktree), "fixture must be a real husk"
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 1
    err = capsys.readouterr().err
    assert "not a registered worktree" in err
    assert "check" in err, "a refusal that does not say what to do next is a wall"
    assert worktree.exists(), "even a real husk is not deleted by this tool"


def test_an_escaping_link_target_survives_the_completion_walker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC--06: the repair must not reintroduce the hazard it completes around."""
    # IMPORTANT: a trailing slash ignores junction directories but not POSIX symlinks;
    # keep the fixture link ignored on both without bypassing the dirty-work refusal.
    repo, worktree, canary = _seed_repo_with_worktree(tmp_path, "node_modules/\n.venv\n")
    if not _internal_junction(worktree) or not _make_directory_link(worktree / ".venv", canary):
        pytest.skip("this platform cannot create a directory link without extra privilege")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) == 0
    assert not worktree.exists()
    assert (canary / "CANARY.txt").exists(), "the walker must not follow what git would have"
    assert (canary / "deep" / "DEEP.txt").exists()


def test_dry_run_reports_the_refusals_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry run is what a command log records, so it has to state what the real run would do."""
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    (worktree / "seed.txt").write_text("edited and never committed", encoding="utf-8")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would refuse" in out and "uncommitted work" in out
    assert "nothing changed" in out
    assert worktree.exists() and _is_registered(repo, worktree)


# --------------------------------------------------------------------------------------------
# Regressions for what the distinct review found.  Every one of these passed against nothing
# before: the first draft had twelve green tests and still deleted real work, because none of
# them constructed a state git itself refuses.
# --------------------------------------------------------------------------------------------


def test_an_untracked_file_is_not_destroyed_by_the_completion_walker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding 1a. The first draft deleted this file and exited zero.

    ``git worktree remove`` refuses a worktree holding untracked, unignored files.  The draft
    asked git only about *tracked* changes, so no refusal fired here, and then it treated git's
    refusal as "the directory is still there, finish the job".
    """
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    precious = worktree / "notes.md"
    precious.write_text("untracked, unignored, never staged", encoding="utf-8")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) != 0
    assert "uncommitted work" in capsys.readouterr().err
    assert precious.exists(), "the file git declined to destroy must still be here"
    assert _is_registered(repo, worktree)


def test_a_locked_worktree_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding 1b. A lock is an explicit instruction not to remove this worktree.

    Nothing the tool checks itself catches a lock, so the containment is the discriminator: git
    deregisters a worktree only once it is past its own checks and has started deleting.  Still
    being registered means git refused, and a refusal is not ours to override.
    """
    repo, worktree, _ = _seed_repo_with_worktree(tmp_path, "node_modules/\n")
    subprocess.run(
        ["git", "worktree", "lock", str(worktree)], cwd=repo, capture_output=True, text=True
    )
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(worktree)]) != 0
    assert "not overriding it" in capsys.readouterr().err
    assert worktree.exists(), "a locked worktree must survive intact"
    assert (worktree / "seed.txt").exists()
    assert _is_registered(repo, worktree)


def test_an_ignored_directory_that_was_never_a_worktree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding 2, in the exact shape that defeated the second attempt at a fix.

    The first fix required two of the directory's top-level entry names to match tracked top-level
    names of the repository.  A real virtualenv has both: ``python -m venv`` writes its own
    ``.gitignore`` into every one it creates, and ``Scripts`` normcase-collides with a tracked
    ``scripts``.  ``.pytest_cache`` likewise, through ``.gitignore`` and ``README.md``.  The
    directory built here carries those markers deliberately -- the earlier fixture omitted the
    auto-generated ``.gitignore`` and so passed against a weaker predicate than the real thing.
    """
    repo, _ = _seed_repo_with_worktree_inside(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.py").write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "scripts"], cwd=repo, capture_output=True, text=True
    )

    venv = repo / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / ".gitignore").write_text("# Created by venv\n*\n", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = somewhere", encoding="utf-8")
    (venv / "Scripts" / "python.exe").write_text("binary", encoding="utf-8")
    _use_fixture_repo(monkeypatch, repo)

    assert main(["remove-worktree", str(venv)]) == 1
    assert "not a registered worktree" in capsys.readouterr().err
    assert (venv / "pyvenv.cfg").exists(), "a virtualenv is not a worktree and is never deleted"
    assert (venv / "Scripts" / "python.exe").exists()


def test_survivor_escape_is_measured_against_the_worktree_not_an_inferred_container(
    tmp_path: Path,
) -> None:
    """Review finding 3. Deregistration silently widened what counted as internal.

    Once git drops the worktree from its list, ``deletable_unit`` falls back to the repository
    root, so a link pointing anywhere inside the repository stops reading as escaping.  The
    survivor census is the one place the container is known exactly.
    """
    from scripts.workspace_link_guard import _escapes_from

    worktree = tmp_path / "wt"
    worktree.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "PRECIOUS.txt").write_text("real content", encoding="utf-8")

    inferred_wrongly = LinkFinding(
        path=worktree / "link", target=elsewhere.resolve(), container=tmp_path.resolve()
    )
    assert not inferred_wrongly.escapes, "the container it was given makes this look internal"
    assert _escapes_from(inferred_wrongly, worktree.resolve()), (
        "stated against the worktree, the same link escapes"
    )


def test_the_walker_unlinks_a_reparse_point_instead_of_descending(tmp_path: Path) -> None:
    """remove_tree in isolation, against the exact shape shutil.rmtree is trusted for elsewhere."""
    from scripts.workspace_link_guard import remove_tree

    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "KEEP.txt").write_text("keep", encoding="utf-8")
    doomed = tmp_path / "doomed"
    (doomed / "real").mkdir(parents=True)
    (doomed / "real" / "f.txt").write_text("go", encoding="utf-8")
    if not _make_directory_link(doomed / "link", outside):
        pytest.skip("this platform cannot create a directory link without extra privilege")

    remove_tree(doomed)

    assert not doomed.exists()
    assert (outside / "deep" / "KEEP.txt").exists(), "the target was reached through the link"
