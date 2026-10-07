"""Refuse filesystem links that let a recursive delete escape the directory it is deleting.

A Windows directory junction inside a git worktree is followed by ``git worktree remove``, which
then deletes the junction target's contents.  When the junction's path is ignored by git the plain,
unforced command does this silently: no diagnostic, no confirmation, no ``--force`` required.  The
same shape on POSIX is an absolute symlink out of the tree.

The detection cannot use ``os.path.islink``.  On Windows that returns ``False`` for a junction --
Python reports ``True`` only for ``IO_REPARSE_TAG_SYMLINK`` -- so a junction is invisible to the
ordinary check and ``os.walk`` descends into it.  This module reads the reparse attribute from
``os.lstat`` instead, and never traverses a reparse point.

Only *escaping* links matter.  pnpm fills ``node_modules`` with hundreds of links into its own
``.pnpm`` store; those resolve back inside the same worktree, are deleted along with everything
else, and detaching them would break the worktree for no benefit.

The same following costs something even when nothing escapes.  Git descends into an *internal*
junction too, deletes the real files behind it, then cannot remove the junction, so the directory
it is emptying is not empty and ``git worktree remove`` fails -- after it has already deregistered
the worktree, leaving a husk no retry can address.  ``--force`` does not help, because that is a
filesystem-level failure rather than a policy refusal.  ``remove-worktree`` therefore finishes the
deletion itself with a walker that unlinks reparse points instead of following them.

Modes::

    python scripts/workspace_link_guard.py census [ROOT]
    python scripts/workspace_link_guard.py check [ROOT]
    python scripts/workspace_link_guard.py remove-worktree PATH [--force] [--dry-run]

``check`` is the gate stage and exits non-zero when an escaping link exists.  Items M23-01, M23-03.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Windows marks junctions and symlinks alike with this attribute; the tag distinguishes them.
# getattr keeps the module importable on platforms where the constant is absent.
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
JUNCTION_TAG = 0xA0000003


@dataclass(frozen=True)
class LinkFinding:
    """One reparse point, with the unit a recursive delete would start from."""

    path: Path
    target: Path
    container: Path

    @property
    def leaves_container(self) -> bool:
        return not _is_within(self.target, self.container)

    @property
    def dangling(self) -> bool:
        return not self.target.exists()

    @property
    def escapes(self) -> bool:
        """Dangerous: deleting the container would follow this link and destroy real content.

        A dangling link is excluded because there is nothing behind it to destroy.  That is not a
        technicality here -- this repository carries 236 dangling links under
        ``frontend/node_modules``, left by pnpm when the checkout was renamed, and failing on them
        would drown the signal.
        """
        return self.leaves_container and not self.dangling

    @property
    def classification(self) -> str:
        if not self.leaves_container:
            return "internal"
        return "dangling" if self.dangling else "ESCAPES"


def _is_within(candidate: Path, root: Path) -> bool:
    a = os.path.normcase(str(candidate))
    b = os.path.normcase(str(root))
    return a == b or a.startswith(b + os.sep)


def is_reparse_point(path: Path) -> bool:
    """True for a junction or a symlink. Deliberately not os.path.islink, which misses junctions."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE) or bool(
        stat.S_ISLNK(metadata.st_mode)
    )


def worktree_roots(repo_root: Path) -> list[Path]:
    """Registered worktree roots, main checkout included. Empty when git is unavailable."""
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    roots = []
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            roots.append(Path(line[len("worktree ") :].strip()).resolve())
    return roots


def deletable_unit(link: Path, repo_root: Path, worktrees: list[Path]) -> Path:
    """The directory a careless recursive delete would most plausibly target.

    The nearest enclosing registered worktree, else the top-level ``.tmp`` entry, else the
    repository root.  A link is dangerous exactly when its target lies outside this unit.
    """
    enclosing = [w for w in worktrees if _is_within(link, w) and w != repo_root]
    if enclosing:
        return max(enclosing, key=lambda w: len(str(w)))
    scratch = repo_root / ".tmp"
    if _is_within(link, scratch) and link != scratch:
        relative = link.relative_to(scratch)
        return scratch / relative.parts[0]
    return repo_root


def find_links(root: Path, repo_root: Path | None = None) -> list[LinkFinding]:
    """Every reparse point under root, without ever traversing one."""
    root = root.resolve()
    repo_root = (repo_root or root).resolve()
    worktrees = worktree_roots(repo_root)
    findings: list[LinkFinding] = []
    visited: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            real = os.path.realpath(directory)
        except OSError:
            continue
        if real in visited:
            continue
        visited.add(real)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if is_reparse_point(path):
                try:
                    target = Path(os.path.realpath(path))
                except OSError:
                    continue
                findings.append(
                    LinkFinding(path, target, deletable_unit(path, repo_root, worktrees))
                )
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
            except OSError:
                continue
    return findings


def detach(link: Path, target: Path) -> bool:
    """Remove the link and not its target.

    ``cmd rmdir`` without a recurse switch unlinks a junction.  ``Remove-Item -Force`` without
    ``-Recurse`` prompts and hangs a non-interactive shell, so it is not used.
    """
    if not target.exists():
        print(f"  refusing: {link} points at {target}, already missing", file=sys.stderr)
        return False
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
    else:
        try:
            link.unlink()
        except OSError:
            pass
    if link.exists() or is_reparse_point(link):
        print(f"  failed to detach {link}", file=sys.stderr)
        return False
    if not target.exists():
        print(f"  {target} disappeared while detaching {link}; stopping", file=sys.stderr)
        return False
    print(f"  detached  {link}  ->  {target}")
    return True


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _census(root: Path) -> int:
    findings = find_links(root, _repo_root())
    for finding in sorted(findings, key=lambda f: str(f.path)):
        print(f"  [{finding.classification}] {finding.path}  ->  {finding.target}")
    escaping = [f for f in findings if f.escapes]
    dangling = [f for f in findings if f.leaves_container and f.dangling]
    internal = len(findings) - len(escaping) - len(dangling)
    print(
        f"\n{len(findings)} reparse point(s): {len(escaping)} escaping, "
        f"{len(dangling)} dangling, {internal} internal"
    )
    return 0


def _check(root: Path) -> int:
    escaping = [f for f in find_links(root, _repo_root()) if f.escapes]
    if not escaping:
        print("workspace link guard: no escaping reparse point")
        return 0
    print("workspace link guard: FAIL", file=sys.stderr)
    for finding in sorted(escaping, key=lambda f: str(f.path)):
        print(f"  {finding.path}", file=sys.stderr)
        print(f"      escapes {finding.container}  ->  {finding.target}", file=sys.stderr)
    print(
        "\nA recursive delete of the containing directory would follow these links and destroy\n"
        "their targets. `git worktree remove` does exactly this, in its plain form as well as\n"
        "with --force, and prints nothing.\n\n"
        "Remedy: do not link into a worktree. Build its environment in place -- a virtualenv\n"
        "costs about 20 seconds and pnpm hardlinks from its global store in about 2. If\n"
        "something must be shared, keep it outside the worktree and use an absolute path.\n"
        "To tear down a worktree that already has links:\n"
        "  python scripts/workspace_link_guard.py remove-worktree <path>",
        file=sys.stderr,
    )
    return 1


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """None when git could not be run at all, which callers must treat as unproven, not as fine."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return None


def is_registered_worktree(path: Path, repo_root: Path) -> bool:
    """Whether path is a worktree git currently lists.

    ``path`` must already be resolved: this compares spellings, and a caller that passes a
    ``..`` segment or a link aliasing a worktree will get the wrong answer.  ``_remove_worktree``
    resolves before calling, and any future caller must too.
    """
    normalized = os.path.normcase(str(path))
    return any(os.path.normcase(str(root)) == normalized for root in worktree_roots(repo_root))


def uncommitted_work(worktree: Path) -> list[str] | None:
    """Porcelain lines for anything unsaved.  None when git could not answer.

    Untracked files are included, and deliberately.  An earlier revision passed
    ``--untracked-files=no``, which meant a file the user had created and never staged raised no
    refusal here -- and the review found that git's own refusal for it was then silently overridden
    downstream, so the file was deleted with a zero exit.  Ignored files stay excluded: that is
    build output, and refusing on it would refuse on every worktree.
    """
    completed = _git("status", "--porcelain", cwd=worktree)
    if completed is None or completed.returncode != 0:
        return None
    return [line for line in completed.stdout.splitlines() if line.strip()]


def unreachable_detached_head(worktree: Path) -> str | None:
    """The commit at risk, or None when removing this worktree loses no history.

    A worktree with a branch checked out is never at risk: ``git worktree remove`` does not delete
    the branch, so its own ref keeps every commit reachable.  History is lost only when ``HEAD`` is
    detached at a commit no ref reaches, which is why this is not phrased as "reachable from some
    ref" -- that formulation passes trivially for every branch worktree and asserts nothing.

    Every path that cannot prove safety returns the risk, matching ``uncommitted_work``.  An
    earlier revision returned None when git could not answer, which made this the one check in the
    module that failed open.
    """
    on_branch = _git("symbolic-ref", "-q", "HEAD", cwd=worktree)
    if on_branch is None:
        return "unknown"
    if on_branch.returncode == 0:
        return None
    revision = _git("rev-parse", "HEAD", cwd=worktree)
    if revision is None or revision.returncode != 0:
        return "unknown"
    commit = revision.stdout.strip()
    reaching = _git("for-each-ref", "--contains", commit, "--format=%(refname)", cwd=worktree)
    if reaching is None or reaching.returncode != 0:
        return commit
    return None if reaching.stdout.strip() else commit


def _unlink_reparse_point(link: Path) -> None:
    """Unlink without following.  ``rmdir`` detaches a junction; ``unlink`` covers file symlinks."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
    if is_reparse_point(link):
        link.unlink()


def _unlink_file(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def remove_tree(root: Path) -> None:
    """Delete a tree, unlinking every reparse point instead of descending into it.

    ``shutil.rmtree`` was probed and does handle reparse points correctly on this build.  It is
    still not used: the correctness of this module rests on not trusting the standard library's
    link handling on Windows, which is the whole defect class M23-01 exists for, and an explicit
    walker keeps the refusal to traverse a link on the same predicate the guard is built from.
    """
    with os.scandir(root) as entries:
        children = sorted(entries, key=lambda e: e.path)
    for child in children:
        path = Path(child.path)
        if is_reparse_point(path):
            _unlink_reparse_point(path)
        elif child.is_dir(follow_symlinks=False):
            remove_tree(path)
        else:
            _unlink_file(path)
    root.rmdir()


def _escapes_from(finding: LinkFinding, container: Path) -> bool:
    """Escape measured against a container that is stated rather than inferred.

    ``LinkFinding.escapes`` measures against ``deletable_unit``, which infers the container from
    the registered worktree list.  Once git has deregistered the target that inference falls back
    to the repository root, and a link pointing anywhere inside the repository stops counting as
    escaping -- so the survivor census silently stopped guarding.  Review found this.  Here the
    container is known exactly, so it is passed in.
    """
    return not _is_within(finding.target, container) and finding.target.exists()


def _report_links(target: Path, escaping: list[LinkFinding]) -> None:
    if escaping:
        print(f"{len(escaping)} escaping reparse point(s) inside {target}:")
        for finding in escaping:
            print(f"  {finding.path}  ->  {finding.target}")
    else:
        print(f"no escaping reparse point inside {target}")


def _work_loss_refusals(worktree: Path) -> list[tuple[str, str]]:
    refusals = []
    unsaved = uncommitted_work(worktree)
    if unsaved is None:
        refusals.append(("uncommitted work", "git could not be asked; treating as unsafe"))
    elif unsaved:
        refusals.append(("uncommitted work", "; ".join(unsaved[:5])))
    commit = unreachable_detached_head(worktree)
    if commit:
        detail = (
            "git could not be asked which refs reach it"
            if commit == "unknown"
            else f"{commit} is reached by no ref"
        )
        refusals.append(("unreachable detached HEAD", detail))
    return refusals


def _remove_worktree(target: Path, force: bool, dry_run: bool) -> int:
    repo_root = _repo_root()
    target = target.resolve()
    if not target.is_dir():
        print(f"not a directory: {target}", file=sys.stderr)
        return 1

    # This tool deletes registered git worktrees and nothing else.  A `--remnant` mode for husks
    # left by earlier failures was built and then removed: recognising "this directory used to be
    # a worktree" has no reliable signal once git has dropped it, and two successive attempts at
    # one admitted real directories.  A rule that guards a recursive delete has to be statable in
    # a sentence, and "it is a registered worktree" is; "it looks like one" is not.
    registered = is_registered_worktree(target, repo_root)
    if not registered:
        print(f"{target} is not a registered worktree; refusing to delete it.", file=sys.stderr)
        print(
            "A husk left by an earlier failed removal is not recognised on purpose. Inspect it\n"
            f"with `workspace_link_guard.py check {target}` and, once that reports no escaping\n"
            "reparse point, delete the directory yourself.",
            file=sys.stderr,
        )
        return 1

    escaping = [f for f in find_links(target, repo_root) if f.escapes]
    _report_links(target, escaping)
    refusals = _work_loss_refusals(target)

    if dry_run:
        for name, detail in refusals:
            print(f"would refuse: {name}: {detail}")
        if refusals and not force:
            print("dry run: nothing changed, and the real run would refuse")
        else:
            if refusals:
                print("--force would override the refusals above")
            print("dry run: nothing changed, and the real run would remove this path")
        return 0

    if refusals:
        for name, detail in refusals:
            if force:
                print(f"--force overriding refusal: {name}: {detail}")
            else:
                print(f"refusing: {name}: {detail}", file=sys.stderr)
        if not force:
            print("Pass --force to remove it anyway.", file=sys.stderr)
            return 1

    for finding in escaping:
        if not detach(finding.path, finding.target):
            return 1

    command = ["git", "worktree", "remove"] + (["--force"] if force else []) + [str(target)]
    print("\n" + " ".join(command))
    completed = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)

    may_finish = False
    if completed.returncode != 0:
        # Which failure this was decides everything, and "the directory is still there" does not
        # distinguish them.  Review found that treating survival alone as licence to delete
        # overrides git's *policy* refusals too: an untracked file it declined to destroy, a
        # worktree the user locked, or the main checkout itself, was deleted with a zero exit.
        #
        # Git deregisters a worktree only once it is past those checks and has begun deleting, so
        # still being registered means it refused rather than failed.  That is the discriminator,
        # and it is behavioural rather than a string match on stderr.
        may_finish = not is_registered_worktree(target, repo_root)
        if not may_finish:
            print(
                f"git refused and left {target} registered and intact; not overriding it",
                file=sys.stderr,
            )
            return completed.returncode

    # Git follows junctions, so an internal one -- every pnpm node_modules has hundreds -- leaves
    # it unable to empty the directory it is deleting, and it has deregistered the worktree by
    # then.  Failing here would strand the caller with a husk no retry can address.
    if may_finish and target.exists():
        survivors = [f for f in find_links(target, repo_root) if _escapes_from(f, target)]
        if survivors:
            print("refusing to finish: an escaping link appeared mid-removal", file=sys.stderr)
            for finding in survivors:
                print(f"  {finding.path}  ->  {finding.target}", file=sys.stderr)
            return 1
        print(f"git left {target} in place; finishing with a junction-aware delete")
        try:
            remove_tree(target)
        except OSError as error:
            print(f"could not finish removing {target}: {error}", file=sys.stderr)
            return 1

    if is_registered_worktree(target, repo_root):
        pruned = _git("worktree", "prune", "-v", cwd=repo_root)
        if pruned is not None and pruned.stdout.strip():
            print(f"pruned stale administrative entry: {pruned.stdout.strip()}")

    if target.exists():
        print(f"{target} still exists after removal", file=sys.stderr)
        return 1
    if is_registered_worktree(target, repo_root):
        print(f"{target} is still a registered worktree after removal", file=sys.stderr)
        return 1
    print(f"removed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("census", "check"):
        node = sub.add_parser(name)
        node.add_argument("root", nargs="?", default=None)
    remove = sub.add_parser("remove-worktree")
    remove.add_argument("path")
    remove.add_argument(
        "--force",
        action="store_true",
        help="override work-loss refusals; not needed for, and no help with, a stuck deletion",
    )
    remove.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "census":
        return _census(Path(args.root) if args.root else _repo_root())
    if args.mode == "check":
        return _check(Path(args.root) if args.root else _repo_root())
    return _remove_worktree(Path(args.path), args.force, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
