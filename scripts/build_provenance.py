"""Emit and validate the deterministic M23-24 package build-provenance record."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.build_provenance import (  # noqa: E402
    BUILD_IDENTITY_SCHEMA,
    BUILD_PROVENANCE_PATH,
    BUILD_PROVENANCE_SCHEMA,
    SERVED_BUNDLE_PATH,
    BuildProvenanceError,
    decode_build_provenance,
)
from comfyui_h3_context.core.product_shell import (  # noqa: E402
    SUPPORTED_FRONTEND_VERSION,
)
from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    validate_directory,
    validate_path_components,
    validate_regular_file,
)

BUILDER_ID = "scripts/frontend_build_report.py"
BUILD_TYPE = "h3-context-frontend-offline/1"
# Single-sourced from the package rather than restated. A second literal can drift from the
# first, and the record would then declare a "resolved" dependency that resolves to nothing while
# --check still passes.
FRONTEND_PACKAGE_VERSION = SUPPORTED_FRONTEND_VERSION
SOURCE_INPUT_FIXED_PATHS = (
    Path("frontend/buildMetadata.ts"),
    Path("frontend/buildProvenance.ts"),
    Path("frontend/package.json"),
    # CRITICAL: the lockfile and the compiler settings decide what the bundle contains. Leaving
    # them out let two bundles built from different dependency resolutions carry an identical
    # embedded identity, which is exactly what this digest exists to make impossible.
    Path("frontend/pnpm-lock.yaml"),
    Path("frontend/tsconfig.json"),
    Path("frontend/vite.config.ts"),
    Path("scripts/build_provenance.py"),
    Path("scripts/frontend_build_report.py"),
    Path("comfyui_h3_context/core/build_provenance.py"),
    Path("governance/contracts/build_provenance_v1.schema.json"),
    Path("comfyui_h3_context/contracts/contract_inventory_v1.json"),
    Path("governance/contracts/public_surface_v1.json"),
)
MAX_SOURCE_INPUT_ENTRIES = 512
MAX_SOURCE_INPUT_FILE_BYTES = 1024 * 1024
MAX_SOURCE_INPUT_TOTAL_BYTES = 16 * 1024 * 1024
JsonObject = dict[str, Any]


class ProvenanceEmissionError(RuntimeError):
    """Raised when deterministic provenance inputs are missing or disagree."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _pretty(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvenanceEmissionError("git provenance authority is unavailable") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ProvenanceEmissionError("git provenance authority is unavailable")
    return value


def _source_timestamp(commit: str) -> str:
    raw = _git("show", "-s", "--format=%cI", commit)
    try:
        parsed = datetime.fromisoformat(raw).astimezone(timezone.utc).replace(microsecond=0)
    except ValueError as exc:
        raise ProvenanceEmissionError("source revision timestamp is invalid") from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_object(path: Path, label: str) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceEmissionError(f"{label} is unavailable or invalid") from exc
    if type(value) is not dict:
        raise ProvenanceEmissionError(f"{label} is not an object")
    return cast(JsonObject, value)


def _source_input_paths() -> tuple[Path, ...]:
    """Return the complete bounded frontend source surface plus fixed build authorities."""

    try:
        paths = [
            validate_regular_file(
                ROOT / relative,
                maximum_bytes=MAX_SOURCE_INPUT_FILE_BYTES,
            )
            for relative in SOURCE_INPUT_FIXED_PATHS
        ]
        source_root = validate_directory(ROOT / "frontend/src")
        directories = [source_root]
        entry_count = len(paths)
        while directories:
            directory = directories.pop()
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                entry_count += 1
                if entry_count > MAX_SOURCE_INPUT_ENTRIES:
                    raise ProvenanceEmissionError("source input inventory exceeds the entry bound")
                # CRITICAL: never hash through a symlink or Windows junction; doing so can disclose
                # files outside the repository and makes the provenance digest host-dependent.
                candidate, metadata = validate_path_components(child)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(candidate)
                elif stat.S_ISREG(metadata.st_mode):
                    if metadata.st_size > MAX_SOURCE_INPUT_FILE_BYTES:
                        raise ProvenanceEmissionError("source input exceeds the file-size bound")
                    paths.append(candidate)
                else:
                    raise ProvenanceEmissionError("source input contains a non-regular entry")
    except (OSError, UnsafePathError) as exc:
        raise ProvenanceEmissionError("source input inventory is unavailable or unsafe") from exc
    if len(paths) == len(SOURCE_INPUT_FIXED_PATHS):
        raise ProvenanceEmissionError("frontend source input inventory is empty")
    return tuple(sorted(paths, key=lambda path: path.relative_to(ROOT).as_posix()))


def _source_inputs_digest() -> str:
    entries: list[JsonObject] = []
    total_bytes = 0
    for path in _source_input_paths():
        relative = path.relative_to(ROOT)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ProvenanceEmissionError(
                f"source input is unavailable: {relative.as_posix()}"
            ) from exc
        total_bytes += len(payload)
        if total_bytes > MAX_SOURCE_INPUT_TOTAL_BYTES:
            raise ProvenanceEmissionError("source input inventory exceeds the byte bound")
        entries.append(
            {"path": relative.as_posix(), "sha256": _sha256_bytes(payload), "size": len(payload)}
        )
    return _sha256_bytes(_canonical(entries))


def _dependencies() -> list[JsonObject]:
    inventory = _json_object(
        ROOT / "comfyui_h3_context/contracts/contract_inventory_v1.json",
        "contract inventory",
    )
    public_surface = _json_object(
        ROOT / "governance/contracts/public_surface_v1.json",
        "public surface",
    )
    dependencies: list[JsonObject] = [
        {
            "uri": "contract:h3-context-contract-inventory/1",
            "digest": inventory.get("fingerprint"),
        },
        {
            "uri": "contract:h3-context-public-surface/1",
            "digest": public_surface.get("fingerprint"),
        },
        {
            "uri": "pkg:pypi/comfyui-frontend-package",
            "version": FRONTEND_PACKAGE_VERSION,
        },
    ]
    if any(type(item.get("digest")) is not str for item in dependencies if "digest" in item):
        raise ProvenanceEmissionError("resolved contract fingerprint is missing")
    return sorted(dependencies, key=lambda item: cast(str, item["uri"]))


def build_record(*, source_commit: str, source_tree: str) -> JsonObject:
    # CRITICAL: `embedded_identity` is read back by frontend/buildProvenance.ts and baked into the
    # next bundle Vite emits, so `--write` must precede the final build and the bundle must then be
    # rebuilt: writing this record after the build ships a bundle carrying a stale
    # source_inputs_sha256 while every generator --check still passes. Only the offline rebuild
    # parity in tests/test_frontend_build_report.py sees it, and only inside the Full Gate.
    try:
        bundle = SERVED_BUNDLE_PATH.read_bytes()
    except OSError as exc:
        raise ProvenanceEmissionError("served bundle is unavailable") from exc
    source_inputs = _source_inputs_digest()
    dependencies = _dependencies()
    dependencies_digest = _sha256_bytes(_canonical(dependencies))
    identity = {
        "schema": BUILD_IDENTITY_SCHEMA,
        "builder_id": BUILDER_ID,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "source_inputs_sha256": source_inputs,
        "resolved_dependencies_sha256": dependencies_digest,
    }
    bundle_entry = {
        "path": "comfyui_h3_context/web/h3-context-sidebar.js",
        "sha256": _sha256_bytes(bundle),
        "size": len(bundle),
    }
    invocation_id = _sha256_bytes(_canonical({"identity": identity, "bundle": bundle_entry}))
    timestamp = _source_timestamp(source_commit)
    return {
        "schema": BUILD_PROVENANCE_SCHEMA,
        "builder": {"id": BUILDER_ID},
        "build_type": BUILD_TYPE,
        "external_parameters": {
            "source_commit": source_commit,
            "source_tree": source_tree,
            "source_inputs_sha256": source_inputs,
        },
        "resolved_dependencies": dependencies,
        "invocation": {
            "id": invocation_id,
            "started_on": timestamp,
            "finished_on": timestamp,
        },
        "bundle": bundle_entry,
        "embedded_identity": identity,
    }


def _recorded_revision() -> tuple[str, str] | None:
    """Return the revision the existing record names, or None when no record file exists.

    CRITICAL: `None` means *absent*, never *unreadable*. Collapsing the two let
    `--reuse-source-revision` answer a corrupt record by silently refreshing to HEAD -- the
    opposite of what that flag asks for -- and it did so without a word. A record that is present
    but cannot be decoded is a failure, not a reason to invent a different answer.
    """

    if not BUILD_PROVENANCE_PATH.is_file():
        return None
    try:
        existing = decode_build_provenance(BUILD_PROVENANCE_PATH.read_bytes())
        parameters = cast(JsonObject, existing["external_parameters"])
        return cast(str, parameters["source_commit"]), cast(str, parameters["source_tree"])
    except (BuildProvenanceError, KeyError, TypeError) as exc:
        raise ProvenanceEmissionError("package provenance record is unreadable") from exc


def _history_is_shallow() -> bool:
    """Report whether this clone lacks the history the revision guard needs."""

    try:
        return _git("rev-parse", "--is-shallow-repository") == "true"
    except ProvenanceEmissionError:
        return False


def _unverifiable_history(reason: str) -> ProvenanceEmissionError:
    """Name a shallow clone as itself instead of accusing the record of being wrong.

    CRITICAL: a shallow clone cannot resolve an older commit's tree or answer `merge-base
    --is-ancestor`, so both guards below fail on a record that is perfectly correct. Failing
    closed is right -- an unverifiable identity must not pass -- but reporting it as "not an
    ancestor" sends the reader after a fabricated revision that does not exist. The remedy is
    full history (`fetch-depth: 0`), and the message has to say so.
    """

    if _history_is_shallow():
        return ProvenanceEmissionError(
            "provenance verification needs full history; this clone is shallow"
        )
    return ProvenanceEmissionError(reason)


def _verify_source_revision(commit: str, tree: str) -> None:
    """Reconcile a recorded revision against Git instead of against the record itself.

    CRITICAL: `--check` used to recompute the expected record from the very fields it was
    checking, so the comparison could not fail and neither field was ever reconciled with Git.
    A record naming a tree that does not exist -- or one belonging to a different commit --
    passed, and the identity shown on the Sidebar could name a tree that provably does not
    contain the bundle shipping beside it. Verify against Git here or the record means nothing.
    """

    try:
        resolved = _git("rev-parse", f"{commit}^{{tree}}")
    except ProvenanceEmissionError:
        raise _unverifiable_history(
            "recorded source commit is unknown to this repository"
        ) from None
    if resolved != tree:
        raise ProvenanceEmissionError("recorded source tree does not belong to the recorded commit")
    head = _git("rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, head],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise _unverifiable_history("recorded source commit is not an ancestor of HEAD")


def _source_revision(*, reuse: bool) -> tuple[str, str]:
    """Choose the source base this record names.

    A record is always written before the commit that carries it, so it can never name its own
    commit; it names the base the build descends from, and `source_inputs_sha256` -- recomputed
    from the live sources on every run -- is what identifies the exact bytes. `--write` therefore
    advances the base to `HEAD`, and only an explicit `--reuse-source-revision` keeps an older one
    for an offline rebuild. Reusing by default is what let the recorded base fall seven commits
    behind the code it described.
    """

    if reuse:
        recorded = _recorded_revision()
        if recorded is not None:
            return recorded
    return _git("rev-parse", "HEAD"), _git("rev-parse", "HEAD^{tree}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--reuse-source-revision", action="store_true")
    args = parser.parse_args(argv)
    if not args.write and not args.check:
        parser.error("one of --write or --check is required")
    try:
        if args.check and not args.write:
            recorded = _recorded_revision()
            if recorded is None:
                raise ProvenanceEmissionError("package provenance record is unreadable")
            source_commit, source_tree = recorded
        else:
            source_commit, source_tree = _source_revision(reuse=args.reuse_source_revision)
        _verify_source_revision(source_commit, source_tree)
        expected = build_record(source_commit=source_commit, source_tree=source_tree)
        decode_build_provenance(_canonical(expected))
        if args.write:
            BUILD_PROVENANCE_PATH.write_bytes(_pretty(expected))
        if args.check:
            actual = decode_build_provenance(BUILD_PROVENANCE_PATH.read_bytes())
            if _canonical(actual) != _canonical(expected):
                raise ProvenanceEmissionError("package provenance does not match current inputs")
        print(_canonical(expected).decode("utf-8"), end="")
    except OSError as exc:
        # CRITICAL: an OSError carries the absolute path it failed on. Formatting it here would
        # write a private path into Full Gate evidence, which AGENTS.md section 3 forbids; name
        # the kind of failure instead.
        print(f"BUILD PROVENANCE: FAIL: provenance record is unreadable ({type(exc).__name__})")
        return 1
    except (BuildProvenanceError, ProvenanceEmissionError) as exc:
        print(f"BUILD PROVENANCE: FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
