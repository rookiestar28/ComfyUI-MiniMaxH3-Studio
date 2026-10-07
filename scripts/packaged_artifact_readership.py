"""Who reads each packaged artifact, computed from readers rather than from what it looks like.

M23-48's plan needs an exact disposition for every packaged contract and dynamically selected font
artifact, because the governance-only set can only be moved once it is known which files are not
governance-only. The question is easy to get wrong in a specific way, and the whole design of this
script is a response to it.

`host_seam_census_v1.json` is the counter-example. It reads like a governance record -- a census of
host seams, hand-maintained, no generator, no `--write` -- and a module under
`frontend/src/host/` imports it, so the bundle compiles it in. Every heuristic short of reading
the imports puts it on the wrong side: "generated versus hand-maintained" does, "record versus
contract" does, and so does any rule about the file's name. M23-47 spent a gate run finding
this out.

So the class of an artifact is decided by **who opens it**, in this order of precedence, because a
file read by more than one area takes the most constraining class:

1. `frontend_source_read` -- imported from `frontend/src/**`, therefore a bundle input. Moving it
   changes the shipped runtime, and editing it changes the bundle digest.
2. `runtime_read` -- opened by `comfyui_h3_context/**/*.py` at run time. Moving it breaks the
   installed package.
3. `tooling_or_evidence_only` -- read only by `scripts/**`, `tests/**` or `frontend/tests/**`. This
   is the candidate governance set, and it is a candidate rather than a disposition.
4. `no_reader` -- opened by nothing at all. Neither "retain" nor "move" is honest for a file nothing
   reads, so these are referred to `scripts/retirement_eligibility.py` rather than swept along with
   the move.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "comfyui_h3_context"
CONTRACTS = f"{PACKAGE}/contracts"
FONTS = f"{PACKAGE}/fonts"
FONT_MANIFEST_PATH = f"{FONTS}/font_manifest_v1.json"
FONT_RUNTIME_READER = f"{PACKAGE}/adapters/authoring_fonts.py"
_EXPECTED_FONT_FACE_COUNT = 4
_MAX_FONT_MANIFEST_BYTES = 512 * 1024
_MAX_READER_SOURCE_BYTES = 1024 * 1024
# CRITICAL: `CONTRACTS` is the directory this generator *classifies* and must keep naming
# the installed package -- the question it answers is which packaged JSON has a reader.
# Its own output has none, so M23-55 moved it out; the two paths are deliberately
# different and collapsing them back would make the census classify its own output.
ARTIFACT_PATH = Path("governance/contracts/packaged_artifact_readership_v1.json")
SCHEMA = "h3.context.packaged_artifact_readership.v1"

Readership = Literal[
    "frontend_source_read", "runtime_read", "tooling_or_evidence_only", "no_reader"
]

#: What each class means for the governance move, one sentence each. The disposition is a function
#: of the class and nothing else, so a file cannot be given a disposition its readers do not allow.
DISPOSITIONS: dict[Readership, str] = {
    "frontend_source_read": "retain: the bundle compiles it in",
    "runtime_read": "retain: the installed package opens it at run time",
    "tooling_or_evidence_only": "movable: no runtime or bundle reader",
    "no_reader": "retain pending retirement review: nothing reads it",
}


@dataclass(frozen=True, slots=True)
class Artifact:
    path: str
    readership: Readership
    readers: tuple[str, ...]

    @property
    def disposition(self) -> str:
        return DISPOSITIONS[self.readership]


class ReadershipError(RuntimeError):
    """The scan could not produce an exact disposition for every packaged artifact."""


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ReadershipError(f"git {' '.join(arguments)} failed")
    return result.stdout


def _contract_artifacts() -> tuple[str, ...]:
    """Every packaged contract JSON, schemas included, as repo-relative POSIX paths."""

    return tuple(
        sorted(
            path.as_posix()
            for path in (ROOT / CONTRACTS).glob("*.json")
            if path.is_file()
            for path in [path.relative_to(ROOT)]
        )
    )


def _read_bounded(path: Path, maximum: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if not 1 <= size <= maximum:
            raise ReadershipError(f"{label} is outside its size bound")
        payload = path.read_bytes()
    except ReadershipError:
        raise
    except OSError as exc:
        raise ReadershipError(f"{label} is unavailable") from exc
    if len(payload) != size:
        raise ReadershipError(f"{label} changed while it was read")
    return payload


def _safe_declared_font_path(value: object, suffix: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ReadershipError("font package_path must be a bounded string")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in value
        or not pure.parts
        or pure.parts[0] != "fonts"
        or pure.suffix != suffix
    ):
        raise ReadershipError("font package_path is outside the closed packaged font profile")

    package_root = ROOT / PACKAGE
    font_root = ROOT / FONTS
    candidate = package_root.joinpath(*pure.parts)
    try:
        resolved_package_root = package_root.resolve(strict=True)
        resolved_font_root = font_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ReadershipError("a manifest-declared font artifact is unavailable") from exc
    if not resolved.is_relative_to(resolved_font_root) or not resolved.is_file():
        raise ReadershipError("a manifest-declared font artifact escapes the font package")
    cursor = resolved_package_root
    for part in pure.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ReadershipError("packaged font artifacts cannot be symbolic links")
    return f"{PACKAGE}/{pure.as_posix()}"


def _font_artifacts() -> tuple[str, ...]:
    """The closed font package, with its allowlist derived from the bounded manifest."""

    manifest_path = ROOT / FONT_MANIFEST_PATH
    try:
        font_root = (ROOT / FONTS).resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ReadershipError("the packaged font manifest is unavailable") from exc
    if (
        not resolved_manifest.is_relative_to(font_root)
        or not resolved_manifest.is_file()
        or manifest_path.is_symlink()
    ):
        raise ReadershipError("the packaged font manifest escapes the font package")
    payload = _read_bounded(manifest_path, _MAX_FONT_MANIFEST_BYTES, "packaged font manifest")
    try:
        manifest = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReadershipError("the packaged font manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ReadershipError("the packaged font manifest must be an object")

    raw_assets = manifest.get("font_assets")
    raw_license = manifest.get("license")
    if not isinstance(raw_assets, list) or not isinstance(raw_license, dict):
        raise ReadershipError("the packaged font manifest has no closed artifact declarations")
    raw_faces: list[object] = []
    for asset in raw_assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("faces"), list):
            raise ReadershipError("the packaged font manifest has an invalid face declaration")
        raw_faces.extend(asset["faces"])
    if len(raw_faces) != _EXPECTED_FONT_FACE_COUNT:
        raise ReadershipError("the packaged font manifest must declare exactly four faces")

    declared = {FONT_MANIFEST_PATH}
    for face in raw_faces:
        if not isinstance(face, dict):
            raise ReadershipError("the packaged font manifest has an invalid face declaration")
        declared.add(_safe_declared_font_path(face.get("package_path"), ".ttf"))
    declared.add(_safe_declared_font_path(raw_license.get("package_path"), ".txt"))
    if len(declared) != _EXPECTED_FONT_FACE_COUNT + 2:
        raise ReadershipError("packaged font artifact paths must be unique")

    # SECURITY: the manifest is the complete package allowlist. An extra font file must not gain
    # runtime authority merely because it was copied into the wheel beside a declared face.
    entries = tuple((ROOT / FONTS).rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ReadershipError("packaged font artifacts cannot be symbolic links")
    actual = {path.relative_to(ROOT).as_posix() for path in entries if path.is_file()}
    if actual != declared:
        raise ReadershipError("the packaged font directory differs from its manifest allowlist")
    return tuple(sorted(declared))


def packaged_artifacts() -> tuple[str, ...]:
    """Every packaged contract and font artifact as repo-relative POSIX paths."""

    return tuple(sorted((*_contract_artifacts(), *_font_artifacts())))


def _search_roots() -> tuple[str, ...]:
    # GUARD: `governance/` is deliberately absent, and adding it would change every answer. The
    # question is who *reads* a packaged artifact, and the governance records name each other and
    # the retained nine because they are records *about* the contracts -- the same distinction
    # `SELF_DESCRIBING_PATHS` draws in `scripts/contract_inventory.py`. Before M23-55 those records
    # sat inside `comfyui_h3_context/` and already did not count, because `_classify` recognises a
    # reader only under `frontend/src`, a package `.py`, or the tooling roots. Searching the new
    # root would hand a fresh reader to artifacts whose only question is whether anyone reads them.
    return ("comfyui_h3_context", "frontend", "scripts", "tests")


#: A document that *names* an artifact does not open it. Excluding prose is not a convenience: it
#: is what keeps this record independent of documentation, and AGENTS.md section 5.1 requires that
#: a prose document stay rewritable without a test turning red. Adding two artifact names to
#: `tests/TEST_SOP.md` made this record stale before the suffixes were excluded, which means a
#: paragraph of prose could fail `test_the_stored_record_matches_a_fresh_scan`.
PROSE_SUFFIXES: tuple[str, ...] = (".md", ".txt", ".rst")


def _classify(reader: str) -> Readership | None:
    """The class a single reader implies, or None when the reader does not count as one."""

    if reader.endswith(PROSE_SUFFIXES):
        return None
    if reader.startswith("frontend/src/"):
        return "frontend_source_read"
    if reader.startswith(f"{PACKAGE}/") and reader.endswith(".py"):
        return "runtime_read"
    if reader.startswith(("scripts/", "tests/", "frontend/tests/", "frontend/")):
        return "tooling_or_evidence_only"
    return None


#: Most constraining first. A file read by both `frontend/src` and `scripts` is a bundle input.
_PRECEDENCE: tuple[Readership, ...] = (
    "frontend_source_read",
    "runtime_read",
    "tooling_or_evidence_only",
)


def _readers_of(names: tuple[str, ...]) -> dict[str, set[str]]:
    """For each artifact file name, the repo-relative files that mention it.

    One `git grep` over the fixed name set rather than one per file: 140 artifacts against four
    search roots is otherwise 560 subprocesses, and the scan is meant to be cheap enough to run in
    a test.
    """

    # GUARD: `git grep` searches tracked files, so a reader that is written but not yet staged is
    # invisible and the record silently omits it. Regenerate after `git add`, not before, or the
    # next `--check` fails for a change that was already made.
    found: dict[str, set[str]] = {name: set() for name in names}
    pattern = re.compile("|".join(re.escape(name) for name in names))
    output = _git(
        "grep", "-n", "-I", "-E", "|".join(re.escape(n) for n in names), "--", *_search_roots()
    )
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        reader, _lineno, text = parts
        for name in pattern.findall(text):
            if reader != f"{CONTRACTS}/{name}":
                found[name].add(reader)
    return found


def _called_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def _subscript_string(node: ast.expr) -> tuple[str, str] | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.value.id, node.slice.value
    return None


def _font_runtime_reader() -> str:
    """Prove that the credited adapter executes the manifest-driven reads."""

    reader_path = ROOT / FONT_RUNTIME_READER
    payload = _read_bounded(reader_path, _MAX_READER_SOURCE_BYTES, "packaged font runtime reader")
    try:
        tree = ast.parse(payload, filename=FONT_RUNTIME_READER)
    except (SyntaxError, ValueError) as exc:
        raise ReadershipError("the packaged font runtime reader is not valid Python") from exc
    loader = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_load_packaged_font_manifest_from_root"
        ),
        None,
    )
    if loader is None:
        raise ReadershipError("the packaged font runtime reader has no manifest loader")

    calls = [node for node in ast.walk(loader) if isinstance(node, ast.Call)]
    safe_arguments = [
        call.args[1]
        for call in calls
        if _called_name(call) == "_safe_package_file" and len(call.args) >= 2
    ]
    dynamic_inputs = {
        value for argument in safe_arguments if (value := _subscript_string(argument)) is not None
    }
    fixed_manifest = any(
        isinstance(argument, ast.JoinedStr)
        and any(
            isinstance(part, ast.Constant) and part.value == "fonts/" for part in argument.values
        )
        and any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id == "FONT_MANIFEST_FILENAME"
            for part in argument.values
        )
        for argument in safe_arguments
    )
    bounded_inputs = {
        argument.id
        for call in calls
        if _called_name(call) == "_read_bounded" and call.args
        for argument in call.args[:1]
        if isinstance(argument, ast.Name)
    }
    if (
        not fixed_manifest
        or len({owner for owner, key in dynamic_inputs if key == "package_path"}) < 2
        or len(bounded_inputs) < 3
    ):
        # CRITICAL: face and license names are data, so grep cannot prove readership. Credit the
        # adapter only while its executable loader resolves and reads every manifest-selected path.
        raise ReadershipError("the packaged font runtime reader lacks executable dynamic reads")
    return FONT_RUNTIME_READER


def scan() -> tuple[Artifact, ...]:
    """Classify every packaged artifact by who opens it."""

    contract_paths = _contract_artifacts()
    font_paths = _font_artifacts()
    paths = tuple(sorted((*contract_paths, *font_paths)))
    names = tuple(Path(path).name for path in contract_paths)
    readers = _readers_of(names)
    font_reader = _font_runtime_reader()
    artifacts: list[Artifact] = []
    for path in paths:
        if path in font_paths:
            artifacts.append(Artifact(path=path, readership="runtime_read", readers=(font_reader,)))
            continue
        name = Path(path).name
        classified = {
            reader: _classify(reader)
            for reader in sorted(readers[name])
            if _classify(reader) is not None
        }
        readership: Readership = "no_reader"
        for candidate in _PRECEDENCE:
            if candidate in classified.values():
                readership = candidate
                break
        artifacts.append(
            Artifact(path=path, readership=readership, readers=tuple(sorted(classified)))
        )
    return tuple(artifacts)


def build_record() -> dict[str, object]:
    artifacts = scan()
    counts: dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact.readership] = counts.get(artifact.readership, 0) + 1
    if len(artifacts) != len(packaged_artifacts()):
        raise ReadershipError("the scan did not cover every packaged artifact")
    return {
        "schema": SCHEMA,
        "artifact_count": len(artifacts),
        "counts": dict(sorted(counts.items())),
        "dispositions": dict(sorted(DISPOSITIONS.items())),
        "movable": sorted(a.path for a in artifacts if a.readership == "tooling_or_evidence_only"),
        "artifacts": [
            {
                "path": artifact.path,
                "readership": artifact.readership,
                "disposition": artifact.disposition,
                "readers": list(artifact.readers),
            }
            for artifact in artifacts
        ],
    }


def _canonical(record: dict[str, object]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _summary(record: dict[str, object]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": "PASS",
        "artifact_count": record["artifact_count"],
        "counts": record["counts"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify packaged artifacts by their readers.")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = _canonical(build_record())
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.write_bytes(expected)
    if args.check and (not target.is_file() or target.read_bytes() != expected):
        print(json.dumps({"schema": SCHEMA, "status": "FAIL"}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(_summary(json.loads(expected)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
