"""Retire scratch checkouts and directories under a workspace's ``.tmp`` from a frozen manifest.

A retirement is irreversible, so nothing here acts on a path, a name or an age.  Every effect is
admitted by one entry of a closed JSON manifest that records what the directory was when it was
inspected -- its filesystem identity, a digest of its content, its Git state -- and, unless the
entry is disposable, a receipt proving its unique content was preserved and restored elsewhere.
The apply mode re-observes all of that immediately before acting and stops on any difference.

The removal sequence, for one admitted entry:

1. rename the entry, in place, to a sibling quarantine name.  On Windows the rename is refused
   while any handle or any process's current directory is inside the subtree, so a successful
   rename is a positive "nobody is using it" proof that a process list cannot give, and afterwards
   no writer can reach the content by its old path;
2. recompute the content and Git digests on the quarantined tree; a mismatch renames it back, a
   match is recorded as ``verified``;
3. for a registered worktree, ``git worktree remove <original path>`` -- the path no longer exists,
   so Git only deregisters that exact entry.  Git never deletes the files: Git for Windows clears
   the read-only attribute of a file before unlinking it, which changes every other hard link to
   the same file;
4. delete the quarantined tree with a walker that unlinks links instead of following them.

Each step is written to the entry's receipt first.  An interrupted entry resumes from its receipt
only; a receipt short of ``verified`` repeats step 2 before anything is deleted.

Modes::

    python scripts/workspace_retirement.py preview --manifest M
    python scripts/workspace_retirement.py apply --manifest M --manifest-sha256 H --entry ID
    python scripts/workspace_retirement.py verify --manifest M

``preview`` and ``verify`` have no filesystem or Git effects.  ``apply`` admits exactly one entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.workspace_link_guard import (
    KNOWN_LINK_TAGS,
    _io_path,
    _linked,
    _unlink_file,
    _unlink_reparse_point,
    link_tag,
    remove_directory,
)

RETIREMENT_MANIFEST_SCHEMA = "h3-context-workspace-retirement-manifest/1"
PRESERVATION_RECEIPT_SCHEMA = "h3-context-workspace-preservation-receipt/1"
RETIREMENT_RECEIPT_SCHEMA = "h3-context-workspace-retirement-receipt/1"
SCRATCH = ".tmp"

KINDS = frozenset({"registered_worktree", "independent_clone", "plain_directory"})
REMOVALS = frozenset(
    {"REMOVE_INTEGRATED", "REMOVE_SUPERSEDED", "REMOVE_DISPOSABLE", "PRESERVE_THEN_REMOVE"}
)
KEEPS = frozenset({"KEEP_ACTIVE", "KEEP_RECOVERY", "KEEP_EXTERNAL", "KEEP_UNRESOLVED"})
TOP_KEYS = frozenset(
    {"schema", "run_id", "receipts", "excluded_dir_names", "excluded_dir_markers", "entries"}
)
ENTRY_KEYS = frozenset(
    {
        "id",
        "path",
        "kind",
        "disposition",
        "rationale",
        "identity",
        "content_digest",
        "git",
        "preservation",
        "citations",
    }
)
GIT_KEYS = frozenset({"head", "branch", "status_digest", "refs_digest"})
IDENTITY_KEYS = frozenset({"device", "inode"})
PRESERVATION_KEYS = frozenset({"receipt", "sha256"})

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_FORBIDDEN_COMPONENT = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class ManifestError(ValueError):
    """The manifest cannot admit anything; nothing was changed."""


class Refused(RuntimeError):
    """This entry is retained; the message says why."""


@dataclass(frozen=True)
class Observation:
    identity: dict[str, int]
    content_digest: str
    git: dict[str, str | None] | None
    nested_git: tuple[str, ...]
    unknown_links: tuple[str, ...] = ()


# ---------------------------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------------------------


def _members(value: object, allowed: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be an object")
    keys = set(value)
    if keys != allowed:
        missing = sorted(allowed - keys)
        unknown = sorted(keys - allowed)
        raise ManifestError(f"{where} members differ: missing {missing}, unknown {unknown}")
    return cast(dict[str, Any], value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where} must be a non-empty string")
    return value


def entry_components(relative: str) -> tuple[str, ...]:
    """The validated components of a workspace-relative entry path strictly beneath ``.tmp``.

    Only ``/`` separators.  Every component is an ordinary name: no traversal, no drive or root,
    no wildcard, stream or device syntax, and no trailing dot or space, which Windows strips and
    which would therefore alias another name.  Prefix matching is never used: ``.tmp-other/x``
    fails here because its first component is not ``.tmp``.
    """
    if not isinstance(relative, str) or not relative:
        raise ManifestError("entry path must be a non-empty string")
    parts = tuple(relative.split("/"))
    if len(parts) < 2 or parts[0] != SCRATCH:
        raise ManifestError(f"entry path is not strictly beneath {SCRATCH}: {relative!r}")
    for part in parts:
        if part in {"", ".", ".."} or _FORBIDDEN_COMPONENT.search(part):
            raise ManifestError(f"entry path has an inadmissible component: {relative!r}")
        if part != part.rstrip(". "):
            raise ManifestError(f"entry path component aliases another name: {relative!r}")
    return parts


def validate_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"manifest is not UTF-8 JSON: {error}") from error
    top = _members(manifest, TOP_KEYS, "manifest")
    if top["schema"] != RETIREMENT_MANIFEST_SCHEMA:
        raise ManifestError("manifest schema is not supported")
    if not isinstance(top["run_id"], str) or not _RUN_ID.match(top["run_id"]):
        raise ManifestError("run_id is invalid")
    receipts = _text(top["receipts"], "receipts")
    if receipts.split("/")[0] == SCRATCH or ".." in receipts.split("/") or ":" in receipts:
        raise ManifestError("receipts must be a workspace-relative directory outside .tmp")
    for key in ("excluded_dir_names", "excluded_dir_markers"):
        names = top[key]
        if not isinstance(names, list) or not all(
            isinstance(n, str) and n and "/" not in n and "\\" not in n for n in names
        ):
            raise ManifestError(f"{key} must be a list of plain names")
    entries = top["entries"]
    if not isinstance(entries, list):
        raise ManifestError("entries must be a list")
    ids: set[str] = set()
    paths: set[str] = set()
    for index, value in enumerate(entries):
        where = f"entries[{index}]"
        entry = _members(value, ENTRY_KEYS, where)
        if not isinstance(entry["id"], str) or not _ID.match(entry["id"]):
            raise ManifestError(f"{where}.id is invalid")
        components = entry_components(entry["path"])
        folded = "/".join(components).casefold()
        if entry["id"] in ids or folded in paths:
            raise ManifestError(f"{where} duplicates an id or a path")
        ids.add(entry["id"])
        paths.add(folded)
        if entry["kind"] not in KINDS:
            raise ManifestError(f"{where}.kind is invalid")
        disposition = entry["disposition"]
        if disposition not in REMOVALS | KEEPS:
            raise ManifestError(f"{where}.disposition is invalid")
        _text(entry["rationale"], f"{where}.rationale")
        if not isinstance(entry["citations"], int) or entry["citations"] < 0:
            raise ManifestError(f"{where}.citations is invalid")
        if disposition in KEEPS:
            continue
        identity = _members(entry["identity"], IDENTITY_KEYS, f"{where}.identity")
        if not all(isinstance(identity[k], int) for k in IDENTITY_KEYS):
            raise ManifestError(f"{where}.identity is invalid")
        if not isinstance(entry["content_digest"], str) or not _SHA256.match(
            entry["content_digest"]
        ):
            raise ManifestError(f"{where}.content_digest is invalid")
        if entry["kind"] == "plain_directory":
            if entry["git"] is not None:
                raise ManifestError(f"{where}.git must be null for a plain directory")
        else:
            git = _members(entry["git"], GIT_KEYS, f"{where}.git")
            if not isinstance(git["head"], str) or not _OID.match(git["head"]):
                raise ManifestError(f"{where}.git.head is invalid")
            if git["branch"] is not None and not isinstance(git["branch"], str):
                raise ManifestError(f"{where}.git.branch is invalid")
            if not isinstance(git["status_digest"], str) or not _SHA256.match(git["status_digest"]):
                raise ManifestError(f"{where}.git.status_digest is invalid")
            refs = git["refs_digest"]
            if (entry["kind"] == "independent_clone") != isinstance(refs, str):
                raise ManifestError(f"{where}.git.refs_digest must be set for clones only")
            if isinstance(refs, str) and not _SHA256.match(refs):
                raise ManifestError(f"{where}.git.refs_digest is invalid")
        if disposition == "REMOVE_DISPOSABLE":
            if entry["kind"] != "plain_directory" or entry["preservation"] is not None:
                raise ManifestError(f"{where}: only a plain directory can be disposable")
        else:
            preservation = _members(entry["preservation"], PRESERVATION_KEYS, where)
            _text(preservation["receipt"], f"{where}.preservation.receipt")
            if not _SHA256.match(str(preservation["sha256"])):
                raise ManifestError(f"{where}.preservation.sha256 is invalid")
    return top


# ---------------------------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------------------------


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=_io_path(cwd) if os.name != "nt" else str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )


def _git_ok(args: Sequence[str], cwd: Path) -> str:
    completed = _git(args, cwd)
    if completed.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])} failed in {cwd.name}: {completed.stderr.strip()}")
    return completed.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_state(entry: Path, kind: str) -> dict[str, str | None]:
    head = _git_ok(["rev-parse", "--verify", "HEAD"], entry).strip()
    branch_run = _git(["symbolic-ref", "-q", "HEAD"], entry)
    branch = branch_run.stdout.strip() if branch_run.returncode == 0 else None
    status = _git(
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=no"], entry
    )
    if status.returncode != 0:
        raise Refused(f"git status failed in {entry.name}")
    refs: str | None = None
    if kind == "independent_clone":
        listing = _git_ok(["for-each-ref", "--format=%(refname) %(objectname)"], entry)
        refs = _sha(listing.encode("utf-8", "surrogateescape"))
    return {
        "head": head,
        "branch": branch,
        "status_digest": _sha(status.stdout.encode("utf-8", "surrogateescape")),
        "refs_digest": refs,
    }


def observe(
    entry: Path,
    kind: str,
    excluded_names: Iterable[str],
    excluded_markers: Iterable[str],
) -> Observation:
    """Identity, content digest and Git state of one entry, read without following any link.

    Every enumeration or stat failure raises: an unreadable directory is never an empty one.
    Directories named in ``excluded_names`` (case-insensitively) or holding one of
    ``excluded_markers`` are recorded by name only -- they are rebuildable environments whose
    churn (bytecode, caches) would otherwise make every digest stale.  A clone's own ``.git``
    directory is covered by its Git digests instead, for the same reason.
    """
    names = {n.casefold() for n in excluded_names}
    markers = tuple(excluded_markers)
    top = os.lstat(_io_path(entry))
    if _linked(top) or not stat.S_ISDIR(top.st_mode):
        raise Refused(f"{entry.name} is not an ordinary directory")
    lines: list[str] = []
    nested: list[str] = []
    unknown: list[str] = []
    stack: list[tuple[Path, str]] = [(entry, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(_io_path(directory)) as listing:
            children = sorted(listing, key=lambda e: e.name)
        for child in children:
            relative = prefix + child.name
            metadata = child.stat(follow_symlinks=False)
            if _linked(metadata):
                tag = link_tag(os.lstat(_io_path(directory / child.name)))
                if os.name == "nt" and tag not in KNOWN_LINK_TAGS:
                    unknown.append(f"{relative} ({tag:#x})")
                try:
                    target = os.readlink(_io_path(directory / child.name))
                except (OSError, ValueError):
                    # A WSL symlink has no Windows-readable target; its tag identifies it.
                    target = f"?tag={tag:#x}"
                lines.append(f"L\t{relative}\t{target}")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if child.name == ".git":
                    if not (prefix == "" and kind == "independent_clone"):
                        nested.append(relative)
                    lines.append(f"G\t{relative}")
                    continue
                path = directory / child.name
                if child.name.casefold() in names or any(
                    os.path.lexists(_io_path(path / marker)) for marker in markers
                ):
                    lines.append(f"X\t{relative}")
                    continue
                lines.append(f"D\t{relative}")
                stack.append((path, relative + "/"))
                continue
            if child.name == ".git" and prefix != "":
                nested.append(relative)
            lines.append(f"F\t{relative}\t{metadata.st_size}\t{metadata.st_mtime_ns}")
    digest = _sha("\n".join(lines).encode("utf-8", "surrogateescape"))
    git = None if kind == "plain_directory" else git_state(entry, kind)
    return Observation(
        identity={"device": int(top.st_dev), "inode": int(top.st_ino)},
        content_digest=digest,
        git=git,
        nested_git=tuple(nested),
        unknown_links=tuple(unknown),
    )


# ---------------------------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------------------------


def registrations(workspace: Path) -> dict[str, bool]:
    """Registered worktree paths (normcased) mapped to whether each is locked."""
    listing = _git_ok(["worktree", "list", "--porcelain", "-z"], workspace)
    result: dict[str, bool] = {}
    current: str | None = None
    for field in listing.split("\0"):
        if field.startswith("worktree "):
            current = os.path.normcase(os.path.normpath(field[len("worktree ") :]))
            result[current] = False
        elif current is not None and (field == "locked" or field.startswith("locked ")):
            result[current] = True
    return result


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def contained_path(workspace: Path, components: Sequence[str]) -> Path:
    """The entry path after proving every ancestor below the workspace is an ordinary directory.

    The workspace root itself is the caller's trust anchor.  Each component from ``.tmp`` down to
    the entry's parent must exist as a plain directory and not a link: a junction anywhere in the
    chain would make the lexical path name a directory somewhere else.
    """
    current = workspace
    for part in components[:-1]:
        current = current / part
        try:
            metadata = os.lstat(_io_path(current))
        except OSError as error:
            raise Refused(f"ancestor {part} cannot be inspected: {error}") from error
        if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise Refused(f"ancestor {part} is not an ordinary directory")
    entry = current / components[-1]
    if _key(Path(os.path.realpath(entry))) != _key(entry):
        raise Refused("entry path is an alias of another location")
    return entry


ProcessReader = Callable[[], list[str]]


def read_processes() -> list[str]:
    """Every process's executable and command line, one line each.  Raises when unreadable."""
    if os.name == "nt":
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-CimInstance Win32_Process | ForEach-Object {"
            " [string]$_.ProcessId + [char]9 + [string]$_.ExecutablePath + [char]9 +"
            " [string]$_.CommandLine }"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            raise Refused("the process list could not be read")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
    else:
        lines = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                raw = Path("/proc", pid, "cmdline").read_bytes().replace(b"\0", b" ")
                parts = [pid, raw.decode("utf-8", "replace")]
                for link in ("exe", "cwd"):
                    try:
                        parts.append(os.readlink(f"/proc/{pid}/{link}"))
                    except OSError:
                        parts.append("")
                lines.append("\t".join(parts))
            except OSError:
                continue
    # CRITICAL: an empty read is not "no process uses it".  A reader that silently returned
    # nothing would admit every entry, so emptiness blocks exactly like a failure does.
    if not lines:
        raise Refused("the process list was empty; refusing to treat it as proof")
    return lines


def _mentions(lines: Iterable[str], relative: str) -> bool:
    needle = relative.replace("\\", "/").casefold()
    for line in lines:
        text = line.replace("\\", "/").casefold()
        start = text.find(needle)
        while start != -1:
            end = start + len(needle)
            if end == len(text) or text[end] in "/\"' \t":
                return True
            start = text.find(needle, start + 1)
    return False


def _check_preservation(workspace: Path, entry: dict[str, Any]) -> None:
    if entry["disposition"] == "REMOVE_DISPOSABLE":
        return
    preservation = entry["preservation"]
    parts = str(preservation["receipt"]).split("/")
    if parts[0] == SCRATCH or ".." in parts or ":" in preservation["receipt"]:
        raise Refused("preservation receipt must live outside .tmp")
    path = workspace.joinpath(*parts)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise Refused(f"preservation receipt is missing: {error}") from error
    if _sha(raw) != preservation["sha256"]:
        raise Refused("preservation receipt digest differs from the manifest")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refused("preservation receipt is not JSON") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != PRESERVATION_RECEIPT_SCHEMA:
        raise Refused("preservation receipt schema is not supported")
    if (
        receipt.get("entry_id") != entry["id"]
        or receipt.get("content_digest") != entry["content_digest"]
    ):
        raise Refused("preservation receipt belongs to another entry or content")
    if receipt.get("status") != "RESTORE_VERIFIED":
        raise Refused("preservation receipt has no verified restore")


def _check_kind(workspace: Path, entry_path: Path, kind: str, registered: dict[str, bool]) -> None:
    dot_git = entry_path / ".git"
    key = _key(entry_path)
    nested = [path for path in registered if path.startswith(key + os.sep)]
    if nested:
        raise Refused("a registered worktree lies inside this entry; handle it first")
    if kind == "registered_worktree":
        if key not in registered:
            raise Refused("entry is not a registered worktree")
        if registered[key]:
            raise Refused("entry is a locked worktree")
        try:
            pointer = Path(_io_path(dot_git)).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise Refused("worktree has no readable .git pointer file") from error
        if not pointer.startswith("gitdir:"):
            raise Refused("worktree .git is not a pointer file")
        admin = Path(pointer[len("gitdir:") :].strip())
        expected_parent = _key(workspace / ".git" / "worktrees")
        if _key(admin.parent) != expected_parent:
            raise Refused("worktree administrative directory is not this workspace's")
        try:
            back = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
        except OSError as error:
            raise Refused("worktree administrative gitdir file is unreadable") from error
        if _key(back) != _key(dot_git):
            raise Refused("worktree administrative directory names another checkout")
    else:
        if key in registered:
            raise Refused("entry is a registered worktree but not declared as one")
        if kind == "independent_clone":
            metadata = os.lstat(_io_path(dot_git)) if os.path.lexists(_io_path(dot_git)) else None
            if metadata is None or _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise Refused("clone has no ordinary .git directory")
        elif os.path.lexists(_io_path(dot_git)):
            raise Refused("plain directory holds a .git entry")


def _compare(entry: dict[str, Any], seen: Observation) -> None:
    if seen.nested_git:
        raise Refused("nested repository: " + ", ".join(seen.nested_git[:3]))
    if seen.unknown_links:
        raise Refused("reparse point of an unproven kind: " + ", ".join(seen.unknown_links[:3]))
    if seen.content_digest != entry["content_digest"]:
        raise Refused("content changed since the manifest was frozen")
    if seen.git != (entry["git"] if entry["git"] is None else dict(entry["git"])):
        raise Refused("Git state changed since the manifest was frozen")


# ---------------------------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------------------------


def delete_tree(root: Path) -> None:
    """Delete a tree without following links and without touching shared file identities.

    Links -- junctions, symlinks, mount points -- are unlinked natively with ``os.unlink``, never
    descended into, and never handed to a shell.  A read-only file is deleted without changing
    its attribute, which other hard links share.
    """
    stack: list[tuple[Path, bool]] = [(root, False)]
    while stack:
        directory, emptied = stack.pop()
        if emptied:
            remove_directory(directory)
            continue
        stack.append((directory, True))
        with os.scandir(_io_path(directory)) as listing:
            children = list(listing)
        for child in children:
            path = directory / child.name
            metadata = child.stat(follow_symlinks=False)
            if _linked(metadata):
                # GUARD: unlink the link object itself.  os.unlink removes a directory junction
                # or symlink without touching its target; descending would delete the target's
                # content, which is exactly the hazard the link guard exists for.  observe() does
                # not look inside excluded directories, so the proven-tag rule (invariant 4) is
                # enforced here as well: an unproven kind stops the deletion with residue.
                tag = link_tag(os.lstat(_io_path(path)))
                if os.name == "nt" and tag not in KNOWN_LINK_TAGS:
                    raise OSError(f"reparse point of an unproven kind ({tag:#x}): {path}")
                _unlink_reparse_point(path)
            elif stat.S_ISDIR(metadata.st_mode):
                stack.append((path, False))
            else:
                _unlink_file(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    raw = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(path.name + ".partial")
    with open(temporary, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@dataclass
class Context:
    workspace: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    process_reader: ProcessReader


def _receipts_dir(context: Context) -> Path:
    parts = str(context.manifest["receipts"]).split("/")
    directory = context.workspace.joinpath(*parts)
    metadata = os.lstat(directory)
    if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ManifestError("receipts directory is not an ordinary directory")
    return directory


def _find_entry(context: Context, entry_id: str) -> dict[str, Any]:
    for entry in context.manifest["entries"]:
        if entry["id"] == entry_id:
            return cast(dict[str, Any], entry)
    raise ManifestError(f"no entry {entry_id!r} in the manifest")


def _quarantine_for(entry_path: Path, run_id: str) -> Path:
    return entry_path.with_name(f"{entry_path.name}.retiring-{run_id}")


def apply_entry(context: Context, entry_id: str) -> dict[str, Any]:
    """Retire one entry.  Returns the final receipt; raises Refused when the entry is retained."""
    entry = _find_entry(context, entry_id)
    if entry["disposition"] not in REMOVALS:
        raise Refused(f"disposition {entry['disposition']} admits no removal")
    workspace = context.workspace
    components = entry_components(entry["path"])
    receipt_path = _receipts_dir(context) / f"{entry_id}.json"
    prior: dict[str, Any] | None = None
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_bytes())
        if prior.get("manifest_sha256") != context.manifest_sha256:
            raise Refused("a receipt from another manifest exists for this entry")
        if prior.get("state") == "completed":
            raise Refused("entry was already retired")
        if prior.get("state") == "restored":
            raise Refused("entry was restored after a mismatch; rebuild the manifest")
    _check_preservation(workspace, entry)
    entry_path = contained_path(workspace, components)
    quarantine = _quarantine_for(entry_path, context.manifest["run_id"])
    kind = entry["kind"]
    receipt: dict[str, Any] = prior or {
        "schema": RETIREMENT_RECEIPT_SCHEMA,
        "manifest_sha256": context.manifest_sha256,
        "entry_id": entry_id,
        "path": entry["path"],
        "quarantine": "/".join([*components[:-1], quarantine.name]),
        "kind": kind,
        "disposition": entry["disposition"],
        "steps": [],
        "state": "admitted",
    }

    def step(state: str, **detail: Any) -> None:
        receipt["state"] = state
        receipt["steps"].append({"state": state, "time": _now(), **detail})
        _write_receipt(receipt_path, receipt)

    def verify_quarantine() -> None:
        # The quarantined tree is compared with the manifest before anything is deleted; a
        # mismatch, or an error while looking, renames it back (a failed rename-back leaves the
        # state short of "verified", so a later resume compares again instead of deleting).
        try:
            seen_after = observe(
                quarantine,
                kind,
                context.manifest["excluded_dir_names"],
                context.manifest["excluded_dir_markers"],
            )
            if seen_after.identity != entry["identity"]:
                raise Refused("quarantined directory identity differs")
            _compare(entry, seen_after)
        except (Refused, OSError) as error:
            os.rename(_io_path(quarantine), _io_path(entry_path))
            step("restored", reason=str(error))
            raise Refused(f"restored after re-observation: {error}") from error
        step("verified")

    prior_state = prior.get("state") if prior else None
    resumable = {"admitted", "quarantined", "verified", "deregistered"}
    resuming = prior_state in resumable and os.path.lexists(_io_path(quarantine))
    if resuming:
        # GUARD ( R1 finding 1): an interrupted entry resumes only from its own receipt, only
        # when the quarantined tree is still the admitted directory, and -- unless the receipt
        # already says "verified" -- only after the quarantined content is compared with the
        # manifest again.  Resuming straight from "quarantined" once deleted a file that arrived
        # after the first observation.  Absence of the original path alone never licenses deletion.
        if os.path.lexists(_io_path(entry_path)):
            raise Refused("both the original and the quarantine path exist; inspect by hand")
        seen = os.lstat(_io_path(quarantine))
        if {"device": seen.st_dev, "inode": seen.st_ino} != entry["identity"]:
            raise Refused("quarantined directory is not the admitted one")
        if prior_state in {"admitted", "quarantined"}:
            verify_quarantine()
    elif prior_state in {"verified", "deregistered"}:
        raise Refused("the receipt names a quarantine path that is missing; inspect by hand")
    else:
        registered = registrations(workspace)
        _check_kind(workspace, entry_path, kind, registered)
        seen_before = observe(
            entry_path,
            kind,
            context.manifest["excluded_dir_names"],
            context.manifest["excluded_dir_markers"],
        )
        if seen_before.identity != entry["identity"]:
            raise Refused("directory identity differs from the manifest")
        _compare(entry, seen_before)
        lines = context.process_reader()
        if _mentions(lines, entry["path"]) or _mentions(
            lines, "/".join([*components[:-1], quarantine.name])
        ):
            raise Refused("a running process references this entry")
        if os.path.lexists(_io_path(quarantine)):
            raise Refused("quarantine path already exists")
        step("admitted", processes_read=len(lines), processes_naming_entry=0)
        try:
            os.rename(_io_path(entry_path), _io_path(quarantine))
        except OSError as error:
            step("retained", reason=f"in use: {error.strerror or error}")
            raise Refused(f"entry is in use (rename refused): {error}") from error
        step("quarantined")
        verify_quarantine()

    if kind == "registered_worktree" and receipt["state"] == "verified":
        # The original path no longer exists, so Git deregisters exactly this entry and deletes
        # no file.  Never --force: a lock refusal must stand.
        completed = _git(["worktree", "remove", str(entry_path)], workspace)
        if completed.returncode != 0 or _key(entry_path) in registrations(workspace):
            os.rename(_io_path(quarantine), _io_path(entry_path))
            step("restored", reason=f"git refused: {completed.stderr.strip()}")
            raise Refused(f"git refused to deregister: {completed.stderr.strip()}")
        step("deregistered")

    try:
        delete_tree(quarantine)
    except OSError as error:
        step(receipt["state"], deletion_error=str(error))
        raise Refused(f"deletion stopped, residue kept in quarantine: {error}") from error
    if os.path.lexists(_io_path(quarantine)) or os.path.lexists(_io_path(entry_path)):
        raise Refused("entry still present after deletion")
    if _key(entry_path) in registrations(workspace):
        raise Refused("entry is still registered after deletion")
    step("completed")
    return receipt


def preview(context: Context) -> list[dict[str, Any]]:
    """Read-only admission of every entry.  The quarantine rename is the one check it cannot run."""
    rows: list[dict[str, Any]] = []
    registered = registrations(context.workspace)
    lines = context.process_reader()
    for entry in context.manifest["entries"]:
        row: dict[str, Any] = {"id": entry["id"], "disposition": entry["disposition"]}
        try:
            entry_path = contained_path(context.workspace, entry_components(entry["path"]))
            if entry["disposition"] in KEEPS:
                row["result"] = "keep" if os.path.lexists(_io_path(entry_path)) else "keep-missing"
            else:
                _check_preservation(context.workspace, entry)
                _check_kind(context.workspace, entry_path, entry["kind"], registered)
                seen = observe(
                    entry_path,
                    entry["kind"],
                    context.manifest["excluded_dir_names"],
                    context.manifest["excluded_dir_markers"],
                )
                if seen.identity != entry["identity"]:
                    raise Refused("directory identity differs from the manifest")
                _compare(entry, seen)
                if _mentions(lines, entry["path"]):
                    raise Refused("a running process references this entry")
                row["result"] = "would-remove"
        except (Refused, OSError) as error:
            row["result"] = "refused"
            row["reason"] = str(error)
        rows.append(row)
    return rows


def verify(context: Context) -> list[dict[str, Any]]:
    """Compare receipts with live state.  Read-only and repeatable."""
    rows: list[dict[str, Any]] = []
    registered = registrations(context.workspace)
    receipts = _receipts_dir(context)
    for entry in context.manifest["entries"]:
        components = entry_components(entry["path"])
        entry_path = context.workspace.joinpath(*components)
        present = os.path.lexists(_io_path(entry_path))
        row: dict[str, Any] = {"id": entry["id"], "disposition": entry["disposition"]}
        receipt_path = receipts / f"{entry['id']}.json"
        state = None
        if receipt_path.exists():
            state = json.loads(receipt_path.read_bytes()).get("state")
        if entry["disposition"] in KEEPS:
            row["result"] = "ok" if present else "VIOLATION: kept entry is missing"
        elif state == "completed":
            quarantine = _quarantine_for(entry_path, context.manifest["run_id"])
            if present or os.path.lexists(_io_path(quarantine)):
                row["result"] = "VIOLATION: retired entry is present"
            elif _key(entry_path) in registered:
                row["result"] = "VIOLATION: retired entry is still registered"
            else:
                row["result"] = "ok"
        elif state is None or state in {"retained", "restored"}:
            row["result"] = "pending" if present else "VIOLATION: absent without a receipt"
        else:
            row["result"] = f"INCOMPLETE: receipt state {state}"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_context(manifest_path: Path, process_reader: ProcessReader | None = None) -> Context:
    raw = manifest_path.read_bytes()
    manifest = validate_manifest(raw)
    return Context(
        workspace=_repo_root(),
        manifest=manifest,
        manifest_sha256=_sha(raw),
        process_reader=process_reader or read_processes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("preview", "verify"):
        modes.add_parser(name).add_argument("--manifest", required=True)
    apply = modes.add_parser("apply")
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--manifest-sha256", required=True)
    apply.add_argument("--entry", required=True)
    args = parser.parse_args(argv)
    try:
        context = load_context(Path(args.manifest))
    except (ManifestError, OSError) as error:
        print(f"workspace retirement: manifest refused: {error}", file=sys.stderr)
        return 2
    if args.mode == "apply":
        if args.manifest_sha256 != context.manifest_sha256:
            print("workspace retirement: manifest digest differs; refusing", file=sys.stderr)
            return 2
        try:
            receipt = apply_entry(context, args.entry)
        except (Refused, ManifestError) as error:
            print(f"workspace retirement: {args.entry}: retained: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"entry": args.entry, "state": receipt["state"]}))
        return 0
    rows = preview(context) if args.mode == "preview" else verify(context)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    if args.mode == "preview":
        refused = [r for r in rows if r["result"] in {"refused", "keep-missing"}]
        return 1 if refused else 0
    bad = [r for r in rows if r["result"].startswith(("VIOLATION", "INCOMPLETE"))]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
