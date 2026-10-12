"""Content-free, read-only observations of explicitly pinned public source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SELECTORS = frozenset({"symbol", "json_pointer", "text_heading"})
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


class AnchorError(ValueError):
    """A source selector or observation is unsafe, ambiguous or unavailable."""


def anchor_digest(text: str, **selector: str) -> str:
    """Hash one explicit source segment without importing or executing its module."""
    if len(selector) != 1 or not set(selector) <= _SELECTORS:
        raise AnchorError("one closed source selector is required")
    kind, value = next(iter(selector.items()))
    # CRITICAL: JSON strings may contain U+2028; splitlines() would turn valid data into control LF.
    if kind != "json_pointer":
        text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    try:
        if kind == "symbol":
            scope: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef = ast.parse(
                text
            )
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value, flags=re.ASCII):
                raise AnchorError("invalid symbol selector")
            for name in value.split("."):
                matches = [
                    node
                    for node in scope.body
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name
                ]
                if len(matches) != 1:
                    raise AnchorError("missing or ambiguous symbol selector")
                scope = matches[0]
            segment = ast.get_source_segment(text, scope)
            if segment is None:
                raise AnchorError("symbol source segment unavailable")
        elif kind == "json_pointer":
            if not re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", value):
                raise AnchorError("invalid JSON pointer selector")
            # IMPORTANT: remove one UTF-8 BOM before parsing; preserve JSON string content.
            selected = json.loads(text.removeprefix("\ufeff"))
            for token in value.split("/")[1:]:
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(selected, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", token):
                        raise AnchorError("invalid array selector")
                    selected = selected[int(token)]
                elif isinstance(selected, dict):
                    selected = selected[token]
                else:
                    raise AnchorError("JSON selector does not resolve")
            segment = json.dumps(
                selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        else:
            if not re.fullmatch(r"#{1,6} [^\r\n]+", value):
                raise AnchorError("invalid heading selector")
            lines = text.splitlines()
            starts = [index for index, line in enumerate(lines) if line == value]
            if len(starts) != 1:
                raise AnchorError("missing or ambiguous heading selector")
            start = starts[0]
            level = len(value.split(" ", 1)[0])
            end = len(lines)
            for index in range(start + 1, len(lines)):
                heading = re.match(r"^(#{1,6}) ", lines[index])
                if heading and len(heading.group(1)) <= level:
                    end = index
                    break
            segment = "\n".join(lines[start:end]).rstrip("\n")
    except (SyntaxError, ValueError, KeyError, IndexError, TypeError) as error:
        if isinstance(error, AnchorError):
            raise
        raise AnchorError("source selector cannot be resolved") from None
    return "sha256:" + hashlib.sha256(segment.encode("utf-8")).hexdigest()


def _source_path(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value
    ):
        raise AnchorError("source path is not bounded")
    if any(part in {".", ".."} for part in PurePosixPath(value).parts):
        raise AnchorError("source path escapes its repository")
    return value


def _git(checkout: Path, *arguments: str) -> bytes:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        return subprocess.check_output(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(checkout), *arguments],
            env=environment,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise AnchorError("pinned source object unavailable") from None


def observe_anchors(
    document: dict[str, Any],
    checkouts: Mapping[str, Path],
    *,
    revisions: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return content-free receipts; later observations never replace the pinned authority."""
    revisions = {} if revisions is None else revisions
    anchors: dict[str, dict[str, Any]] = {}
    try:
        entries = document["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
            raise AnchorError("bounded catalogue entries required")
        for entry in entries:
            for anchor in entry["anchors"]:
                previous = anchors.get(anchor["anchor_id"])
                if previous is not None and previous != anchor:
                    raise AnchorError("ambiguous anchor identity")
                _source_path(anchor["path"])
                revision = revisions.get(anchor["repository"], anchor["revision"])
                if (
                    _REVISION.fullmatch(revision) is None
                    or _REVISION.fullmatch(anchor["revision"]) is None
                ):
                    raise AnchorError("full source revision required")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", anchor["content_sha256"]):
                    raise AnchorError("malformed expected source hash")
                selector = {name: anchor[name] for name in _SELECTORS if name in anchor}
                if len(selector) != 1:
                    raise AnchorError("one source selector required")
                anchors[anchor["anchor_id"]] = anchor
        if not set(revisions) <= {anchor["repository"] for anchor in anchors.values()}:
            raise AnchorError("unregistered revision override")
    except (KeyError, TypeError):
        raise AnchorError("incomplete source anchor") from None
    observed: list[dict[str, str]] = []
    cache: dict[tuple[str, str, str], str] = {}
    for identity, anchor in sorted(anchors.items()):
        repository = anchor["repository"]
        if repository not in checkouts:
            raise AnchorError("source checkout unavailable")
        revision = revisions.get(repository, anchor["revision"])
        path = anchor["path"]
        key = (repository, revision, path)
        if key not in cache:
            checkout = checkouts[repository]
            # CRITICAL: use exact Git objects; importing reference modules could run untrusted code.
            if _git(checkout, "cat-file", "-t", revision).strip() != b"commit":
                raise AnchorError("source authority must name a commit")
            size = int(_git(checkout, "cat-file", "-s", f"{revision}:{path}").strip())
            if not 0 < size <= _MAX_SOURCE_BYTES:
                raise AnchorError("source object exceeds observation budget")
            try:
                cache[key] = _git(checkout, "show", f"{revision}:{path}").decode("utf-8")
            except UnicodeDecodeError:
                raise AnchorError("source object is not UTF-8 text") from None
        selector = {name: anchor[name] for name in _SELECTORS if name in anchor}
        actual = anchor_digest(cache[key], **selector)
        expected = anchor["content_sha256"]
        observed.append(
            {
                "anchor_id": identity,
                "repository": repository,
                "revision": revision,
                "expected_sha256": expected,
                "observed_sha256": actual,
                "status": "MATCH" if actual == expected else "CHANGED",
            }
        )
    return observed


def copied_lines(
    paths: Sequence[Path], hashes: Collection[str], exemptions: Mapping[str, str]
) -> list[tuple[Path, int, str]]:
    """Flag significant normalized line recurrence, admitting only individually reasoned hashes."""
    if any(_HASH.fullmatch(digest) is None for digest in hashes):
        raise AnchorError("invalid source line hash")
    if any(
        digest not in hashes or not isinstance(reason, str) or not reason.strip()
        for digest, reason in exemptions.items()
    ):
        raise AnchorError("source line exemptions must be individually reasoned")
    found: list[tuple[Path, int, str]] = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            normalized = line.strip()
            if len(normalized) < 20:
                continue
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if digest in hashes and digest not in exemptions:
                found.append((path, number, digest))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout", action="append", required=True, metavar="PUBLIC_REPO=LOCAL_PATH"
    )
    parser.add_argument("--revision", action="append", default=[], metavar="PUBLIC_REPO=FULL_SHA")
    args = parser.parse_args()
    try:
        checkouts: dict[str, Path] = {}
        revisions: dict[str, str] = {}
        for value in args.checkout:
            repository, path = value.split("=", 1)
            if repository in checkouts or not path:
                raise AnchorError("ambiguous checkout input")
            checkouts[repository] = Path(path)
        for value in args.revision:
            repository, revision = value.split("=", 1)
            if repository in revisions:
                raise AnchorError("ambiguous revision input")
            revisions[repository] = revision
        root = Path(__file__).resolve().parents[1]
        document = json.loads(
            (root / "governance/contracts/native_behaviour_catalogue_v1.json").read_bytes()
        )
        observations = observe_anchors(document, checkouts, revisions=revisions)
    except (AnchorError, ValueError, OSError):
        print(json.dumps({"schema": "h3.source_anchor_observation.v1", "status": "REFUSED"}))
        return 2
    changed = any(row["status"] != "MATCH" for row in observations)
    print(
        json.dumps(
            {
                "schema": "h3.source_anchor_observation.v1",
                "status": "CHANGED" if changed else "MATCH",
                "anchors": observations,
            },
            indent=2,
        )
    )
    return int(changed)


if __name__ == "__main__":
    raise SystemExit(main())
