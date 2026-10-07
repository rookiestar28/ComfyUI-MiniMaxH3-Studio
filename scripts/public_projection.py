"""Build an exact public Git tree without changing the source index or checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

# Direct invocation and public checkouts must resolve the sibling helper from this tree.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.product_completeness import (
    CompletenessError,
    require_present,
    required_paths,
)

# Compatibility exports preserve existing projection/Registry consumers.
from scripts.public_source_policy import (  # noqa: F401
    DIRECTORIES,
    FILES,
    MAINTAINER_DOCUMENTS,
    PRIVATE_FILES,
    PRIVATE_PARTS,
    PUBLICATION_FILES,
    allowed,
)

__all__ = [
    "DIRECTORIES",
    "FILES",
    "MAINTAINER_DOCUMENTS",
    "PRIVATE_FILES",
    "PRIVATE_PARTS",
    "PUBLICATION_FILES",
    "allowed",
    "ProjectionError",
    "entries",
    "exact_commit",
    "git",
    "project",
    "snapshot",
]

ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"[0-9a-f]{40}")


class ProjectionError(ValueError):
    """The selected source cannot form a safe public projection."""


def git(
    root: Path, *args: str, data: bytes | None = None, environment: dict[str, str] | None = None
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=data,
        capture_output=True,
        env=environment,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise ProjectionError("Git projection operation failed")
    return result.stdout


def exact_commit(root: Path, value: str) -> str:
    if FULL_SHA.fullmatch(value) is None or value == "0" * 40:
        raise ProjectionError("commit must be a non-zero full SHA")
    if git(root, "rev-parse", "--verify", value + "^{commit}").decode().strip() != value:
        raise ProjectionError("commit identity does not match")
    return value


def entries(root: Path, revision: str) -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}
    for row in git(root, "ls-tree", "-r", "-z", "--full-tree", revision).decode().split("\0"):
        if row:
            metadata, path = row.split("\t", 1)
            mode, kind, oid = metadata.split()
            result[path] = (mode, kind, oid)
    return result


def snapshot(root: Path) -> tuple[bytes, bytes]:
    return git(root, "rev-parse", "HEAD"), git(root, "ls-files", "-s", "-z")


def _blobs(root: Path, bindings: dict[str, tuple[str, str, str]]) -> dict[str, bytes]:
    """Batch exact blobs: one Git process, without checkout or path interpretation."""
    oids = sorted({value[2] for value in bindings.values()})
    if not oids:
        return {}
    raw = git(root, "cat-file", "--batch", data=("\n".join(oids) + "\n").encode())
    values: dict[str, bytes] = {}
    cursor = 0
    for expected in oids:
        boundary = raw.index(b"\n", cursor)
        oid, kind, size = raw[cursor:boundary].split()
        if oid.decode() != expected or kind != b"blob":
            raise ProjectionError("source blob batch identity does not match")
        cursor = boundary + 1
        length = int(size)
        values[expected] = raw[cursor : cursor + length]
        cursor += length + 1
    if cursor != len(raw):
        raise ProjectionError("source blob batch is malformed")
    return {path: values[value[2]] for path, value in bindings.items()}


def project(
    root: Path, *, source: str, parent: str, output: Path, local_ref: str | None = None
) -> dict[str, object]:
    """Keep only allowed regular blobs; expose an optional ref after all preservation checks."""
    root, output = root.resolve(), output.absolute()
    if not output.is_relative_to(root) or output == root or ".." in output.parts or output.exists():
        raise ProjectionError("projection output must be fresh and inside the workspace")
    for ancestor in (output.parent, *output.parent.parents):
        if ancestor == root:
            break
        if ancestor.exists():
            metadata = ancestor.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ProjectionError("projection output contains a link or reparse point")
    source, parent = exact_commit(root, source), exact_commit(root, parent)
    source_entries, parent_entries = entries(root, source), entries(root, parent)
    if any(not allowed(path) for path in parent_entries):
        raise ProjectionError("public predecessor contains an excluded path")
    selected = {path: entry for path, entry in source_entries.items() if allowed(path)}
    source_payloads = _blobs(
        root,
        {
            path: value
            for path, value in source_entries.items()
            if path.endswith(".py") or path in {"pyproject.toml", ".github/workflows/publish.yml"}
        },
    )
    try:
        required = required_paths(
            source_entries,
            source_payloads.__getitem__,
            publication=True,
        )
        require_present(required, selected, phase="public projection")
    except CompletenessError as exc:
        raise ProjectionError(str(exc)) from exc
    if not selected or any(
        mode not in {"100644", "100755"} or kind != "blob" for mode, kind, _ in selected.values()
    ):
        raise ProjectionError("projection requires regular Git blobs")
    if len({path.casefold() for path in selected}) != len(selected):
        raise ProjectionError("projection contains a case-normalized collision")
    ref = None if local_ref is None else "refs/heads/" + local_ref
    if ref is not None:
        git(root, "check-ref-format", ref)
        probe = subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if probe.returncode != 1:
            raise ProjectionError("projection ref must not already exist")
    before = snapshot(root)
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="projection-", dir=output) as directory:
        temporary = Path(directory)
        policy_root = temporary / "policy"
        policy_root.mkdir()
        for path, (mode, kind, oid) in source_entries.items():
            if PurePosixPath(path).name != ".gitignore":
                continue
            if (
                path.startswith("/")
                or ":" in path
                or "\\" in path
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
            ):
                raise ProjectionError("candidate ignore policy path is unsafe")
            if (
                mode not in {"100644", "100755"}
                or kind != "blob"
                or ".." in PurePosixPath(path).parts
            ):
                raise ProjectionError("candidate ignore policy is not a regular blob")
            target = policy_root.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(root, "cat-file", "blob", oid))
        policy = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "--work-tree=" + str(policy_root),
                "check-ignore",
                "--no-index",
                "--stdin",
                "-z",
            ],
            input="\0".join(selected).encode() + b"\0",
            capture_output=True,
            check=False,
            timeout=30,
        )
        # CRITICAL: allowlisting is never permission to force a gitignored product path public.
        if policy.returncode != 1 or policy.stdout:
            raise ProjectionError("projection contains an ignored path or ignore check failed")
        environment = dict(os.environ, GIT_INDEX_FILE=str(temporary / "index"))
        git(root, "read-tree", "--empty", environment=environment)
        rows = "".join(
            f"{mode} {oid}\t{path}\0" for path, (mode, _, oid) in sorted(selected.items())
        )
        git(root, "update-index", "-z", "--index-info", data=rows.encode(), environment=environment)
        tree = git(root, "write-tree", environment=environment).decode().strip()
    commit = (
        git(root, "commit-tree", tree, "-p", parent, "-m", "build: prepare public source")
        .decode()
        .strip()
    )
    if entries(root, commit) != selected or snapshot(root) != before:
        raise ProjectionError("projection blob or source preservation check failed")
    parents = git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    if parents != [parent]:
        raise ProjectionError("projection predecessor does not match")
    selected_payloads = _blobs(root, selected)
    receipt: dict[str, object] = {
        "schema": "h3-context-public-projection/1",
        "status": "PASS",
        "source_commit": source,
        "public_parent": parent,
        "public_commit": commit,
        "public_tree": tree,
        "source_index_head_unchanged": True,
        "paths": {
            path: {
                "mode": mode,
                "blob": oid,
                "sha256": hashlib.sha256(selected_payloads[path]).hexdigest(),
            }
            for path, (mode, _, oid) in sorted(selected.items())
        },
    }
    (output / "projection.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if ref is not None:
        # CRITICAL: CAS-create a local ref only after exact blobs and real index/HEAD are proved.
        git(root, "update-ref", ref, commit, "0" * 40)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--public-parent", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-ref")
    args = parser.parse_args(argv)
    try:
        receipt = project(
            ROOT,
            source=args.source,
            parent=args.public_parent,
            output=args.output,
            local_ref=args.local_ref,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Public projection: FAIL")
        return 1
    print(json.dumps({key: value for key, value in receipt.items() if key != "paths"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
