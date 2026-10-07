"""Compare an explicitly supplied installed runtime with the repository-owned runtime tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    validate_directory,
    validate_path_components,
)

MAX_FILES = 2_048
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
ALLOWED_SUFFIXES = {".py", ".json", ".js", ".typed"}
JsonObject = dict[str, Any]


class RuntimeParityError(RuntimeError):
    """Raised when either tree is unsafe, unbounded or structurally invalid."""


def _inventory(root: Path) -> dict[str, str]:
    try:
        root = validate_directory(root)
        package = validate_directory(root / "comfyui_h3_context")
    except UnsafePathError as exc:
        raise RuntimeParityError("runtime root is unavailable or unsafe") from exc
    inventory: dict[str, str] = {}
    total = 0
    stack = [package]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeParityError("runtime tree is unreadable") from exc
        for child in children:
            try:
                # CRITICAL: never follow installed-runtime links or junctions; they can escape the
                # explicitly supplied root and leak unrelated files into parity evidence.
                child, metadata = validate_path_components(child)
            except UnsafePathError as exc:
                raise RuntimeParityError("runtime tree contains a link or special entry") from exc
            if stat.S_ISDIR(metadata.st_mode):
                if child.name not in {"__pycache__"}:
                    stack.append(child)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeParityError("runtime tree contains a special entry")
            if child.suffix not in ALLOWED_SUFFIXES and child.name != "py.typed":
                continue
            if not 0 <= metadata.st_size <= MAX_FILE_BYTES:
                raise RuntimeParityError("runtime file exceeds the byte bound")
            total += metadata.st_size
            if total > MAX_TOTAL_BYTES or len(inventory) >= MAX_FILES:
                raise RuntimeParityError("runtime inventory exceeds its resource bound")
            relative = child.relative_to(root).as_posix()
            inventory[relative] = f"sha256:{hashlib.sha256(child.read_bytes()).hexdigest()}"
    if not inventory:
        raise RuntimeParityError("runtime inventory is empty")
    return dict(sorted(inventory.items()))


def compare_runtime_trees(repository_root: Path, installed_root: Path) -> JsonObject:
    """Return a content-free comparison; root paths never enter the result."""

    expected = _inventory(repository_root)
    observed = _inventory(installed_root)
    differences: list[JsonObject] = []
    for path in sorted(set(expected) | set(observed)):
        expected_digest = expected.get(path)
        observed_digest = observed.get(path)
        if expected_digest is None:
            differences.append({"path": path, "kind": "extra", "observed_sha256": observed_digest})
        elif observed_digest is None:
            differences.append(
                {"path": path, "kind": "missing", "expected_sha256": expected_digest}
            )
        elif expected_digest != observed_digest:
            differences.append(
                {
                    "path": path,
                    "kind": "content",
                    "expected_sha256": expected_digest,
                    "observed_sha256": observed_digest,
                }
            )
    return {
        "schema": "h3-context-runtime-parity/1",
        "status": "PARITY" if not differences else "MISMATCH",
        "repository_label": "repository-runtime",
        "installed_label": "installed-runtime",
        "expected_file_count": len(expected),
        "observed_file_count": len(observed),
        "differences": differences,
    }


def _self_check() -> JsonObject:
    # `.tmp/` is gitignored, so it does not exist on a fresh clone and no earlier gate stage
    # creates it; without this the first Full Gate on a new checkout fails here.
    (ROOT / ".tmp").mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="h3-runtime-parity-", dir=ROOT / ".tmp"))
    try:
        repository = temporary / "repository"
        installed = temporary / "installed"
        package = repository / "comfyui_h3_context"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        shutil.copytree(repository, installed)
        parity = compare_runtime_trees(repository, installed)
        (installed / "comfyui_h3_context" / "__init__.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        mismatch = compare_runtime_trees(repository, installed)
        if parity["status"] != "PARITY" or mismatch["status"] != "MISMATCH":
            raise RuntimeParityError("runtime parity self-check did not discriminate stale bytes")
        return {
            "schema": "h3-context-runtime-parity-self-check/1",
            "status": "PASS",
            "parity_file_count": parity["expected_file_count"],
            "mismatch_count": len(mismatch["differences"]),
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=False)


def _error_result(reason: str) -> str:
    """Render an ERROR result whose text can never carry a path."""

    return json.dumps(
        {"schema": "h3-context-runtime-parity/1", "status": "ERROR", "error": reason},
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-runtime", type=Path)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_check:
            if args.installed_runtime is not None:
                raise RuntimeParityError("--self-check cannot compare an installed runtime")
            result = _self_check()
        else:
            if args.installed_runtime is None:
                raise RuntimeParityError("--installed-runtime is required and never discovered")
            result = compare_runtime_trees(args.repository_root, args.installed_runtime)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if result["status"] in {"PASS", "PARITY"} else 1
    except OSError:
        # CRITICAL: an OSError names the absolute path it failed on, and this object is printed
        # into Full Gate evidence. Emitting `str(exc)` would leak the installed runtime's private
        # path, which AGENTS.md section 3 forbids and which this module's own docstring promises
        # never happens. Report a closed reason instead.
        print(_error_result("runtime_root_unreadable"))
        return 2
    except RuntimeParityError as exc:
        # CRITICAL: this is the one output path that forwards an exception's own text, so every
        # RuntimeParityError message in this module must stay a path-free constant. An f-string
        # naming the root or the offending file would put a private path into Full Gate evidence
        # through here; `tests/test_build_provenance.py` asserts the constancy structurally.
        print(_error_result(str(exc)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
