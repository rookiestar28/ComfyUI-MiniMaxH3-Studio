"""Audit the actual local Comfy Registry pack archive without publishing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import IO, Any

# CRITICAL: publication preflight must not create force-included bytecode before archive audit.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct script execution must import this exact worktree, not a stale installed package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    prepare_regular_output,
    validate_directory,
    validate_regular_file,
)
from scripts.product_completeness import (  # noqa: E402
    CompletenessError,
    checkout_required_paths,
    require_present,
    smoke_archive,
)

MAX_FILES = 4_096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_MEMBER_NAME_CHARS = 1_024
MAX_PATH_COMPONENT_CHARS = 255
READ_CHUNK_BYTES = 1024 * 1024
TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".mjs",
        ".json",
        ".md",
        ".py",
        ".svg",
        ".txt",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
        ".toml",
        ".ps1",
        ".sh",
    }
)
TEXT_NAMES = frozenset({"license", "notice", "readme"})
ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
PRIVATE_PATH_PARTS = frozenset(
    {
        ".git",
        ".planning",
        ".sessions",
        ".tmp",
        "node_modules",
        "reference",
        ".reference",
        "__pycache__",
    }
)
BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})
WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
FORBIDDEN_CONTENT_MARKERS = (
    ".planning/",
    ".sessions/",
    "reference/docs/",
    "reference/ui/",
    "agents.md",
    "roadmap.md",
    "independent_review_attempt",
    "review_attestation_attempt",
)
RUNTIME_MEMBER = "comfyui_h3_context/web/h3-context-sidebar.js"

JsonObject = dict[str, Any]


class RegistryPayloadError(RuntimeError):
    """Raised when the actual local Registry archive is unsafe or ambiguous."""


def _hash_stream(
    stream: IO[bytes], *, maximum_bytes: int, label: str, capture: bool
) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    payload = bytearray() if capture else None
    total = 0
    while True:
        try:
            chunk = stream.read(min(READ_CHUNK_BYTES, maximum_bytes + 1 - total))
        except OSError as exc:
            raise RegistryPayloadError(f"{label} cannot be read") from exc
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise RegistryPayloadError(f"{label} size is outside the bound")
        digest.update(chunk)
        if payload is not None:
            payload.extend(chunk)
    return "sha256:" + digest.hexdigest(), total, b"" if payload is None else bytes(payload)


def _hash_regular_file(path: Path, *, maximum_bytes: int, label: str) -> tuple[str, int]:
    try:
        source = validate_regular_file(path, maximum_bytes=maximum_bytes)
        with source.open("rb") as stream:
            digest, size, _ = _hash_stream(
                stream, maximum_bytes=maximum_bytes, label=label, capture=False
            )
    except UnsafePathError as exc:
        if "link or reparse" in str(exc):
            detail = "contains a link or reparse component"
        elif "size is outside" in str(exc):
            detail = "size is outside the bound"
        else:
            detail = "is missing or unavailable"
        raise RegistryPayloadError(f"{label} {detail}") from exc
    except OSError as exc:
        raise RegistryPayloadError(f"{label} cannot be read") from exc
    return digest, size


def _run_git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RegistryPayloadError("Git tracking inventory is unavailable") from exc
    if result.returncode != 0:
        raise RegistryPayloadError("Git tracking inventory is unavailable")
    return result.stdout


def _git_tracked_paths(root: Path) -> frozenset[str]:
    """Return the exact worktree index paths without interpreting shell or Git quoting."""

    try:
        safe_root = validate_directory(root)
        top_level = _run_git(safe_root, "rev-parse", "--show-toplevel").decode(
            "utf-8", errors="strict"
        )
        reported_root = Path(top_level.strip())
        if os.path.normcase(os.path.abspath(reported_root)) != os.path.normcase(
            os.path.abspath(safe_root)
        ):
            raise RegistryPayloadError("Git tracking inventory belongs to a different worktree")
        raw = _run_git(safe_root, "ls-files", "-z")
        decoded = raw.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnsafePathError) as exc:
        raise RegistryPayloadError("Git tracking inventory is invalid") from exc
    if not decoded.endswith("\x00"):
        raise RegistryPayloadError("Git tracking inventory is not NUL-terminated")
    paths = decoded[:-1].split("\x00")
    tracked: set[str] = set()
    for path in paths:
        normalized = _canonical_relative_path(path, label="Git-tracked path")
        if normalized in tracked:
            raise RegistryPayloadError("Git tracking inventory contains a duplicate path")
        tracked.add(normalized)
    if not tracked:
        raise RegistryPayloadError("Git tracking inventory is empty")
    return frozenset(tracked)


def _canonical_relative_path(value: str, *, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not value
        or len(value) > MAX_MEMBER_NAME_CHARS
        or normalized != value
        or "\x00" in value
        or any(not character.isprintable() for character in value)
        or "\\" in value
        or value.startswith("/")
        or ":" in value
    ):
        raise RegistryPayloadError(f"{label} has an unsafe or non-canonical name")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RegistryPayloadError(f"{label} has an unsafe path")
    for part in parts:
        basename = part.split(".", 1)[0].casefold()
        if (
            len(part) > MAX_PATH_COMPONENT_CHARS
            or part.endswith((".", " "))
            or basename in WINDOWS_RESERVED_BASENAMES
        ):
            raise RegistryPayloadError(f"{label} has an unsafe or non-canonical name")
    return normalized


def _canonical_member_path(info: zipfile.ZipInfo) -> str:
    original = info.orig_filename
    if info.is_dir() or original.endswith("/"):
        raise RegistryPayloadError("Registry archive contains a directory member")
    if original != info.filename:
        raise RegistryPayloadError("Registry archive member has an unsafe or non-canonical name")
    return _canonical_relative_path(original, label="Registry archive member")


def _validate_member_policy(relative: str) -> None:
    parts = relative.split("/")
    folded_parts = tuple(part.casefold() for part in parts)
    if "__pycache__" in folded_parts or Path(parts[-1]).suffix.casefold() in BYTECODE_SUFFIXES:
        raise RegistryPayloadError(
            f"Registry archive contains bytecode or cache material: {relative}"
        )
    if any(part in PRIVATE_PATH_PARTS for part in folded_parts):
        raise RegistryPayloadError(f"Registry archive contains a private path: {relative}")
    from scripts.public_projection import allowed

    # IMPORTANT: developer trees are public, but cannot admit private SOPs, dependency
    # installs, ignored files or arbitrary workflow files through that broader boundary.
    if not allowed(relative):
        raise RegistryPayloadError(f"Registry archive contains a private path: {relative}")


def _validate_member_metadata(info: zipfile.ZipInfo, relative: str) -> None:
    if info.flag_bits & 0x1:
        raise RegistryPayloadError(f"Registry archive contains an encrypted member: {relative}")
    if info.compress_type not in ALLOWED_COMPRESSION:
        raise RegistryPayloadError(f"Registry archive uses unsupported compression: {relative}")
    if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
        raise RegistryPayloadError(f"Registry archive member size is outside the bound: {relative}")
    if info.compress_size < 0 or info.compress_size > MAX_ARCHIVE_BYTES:
        raise RegistryPayloadError(
            f"Registry archive compressed member size is outside the bound: {relative}"
        )
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise RegistryPayloadError(f"Registry archive contains a symlink member: {relative}")
    if file_type not in {0, stat.S_IFREG}:
        raise RegistryPayloadError(f"Registry archive member is not a regular file: {relative}")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, relative: str) -> bytes:
    try:
        with archive.open(info, "r") as stream:
            _, observed_size, payload = _hash_stream(
                stream,
                maximum_bytes=MAX_FILE_BYTES,
                label=f"Registry archive member {relative}",
                capture=True,
            )
    except (
        EOFError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zlib.error,
        NotImplementedError,
    ) as exc:
        raise RegistryPayloadError(f"Registry archive member is corrupt: {relative}") from exc
    if observed_size != info.file_size:
        raise RegistryPayloadError(
            f"Registry archive member size disagrees with content: {relative}"
        )
    if observed_size == 0 and relative != "comfyui_h3_context/py.typed":
        raise RegistryPayloadError(f"Registry archive contains an empty member: {relative}")
    return payload


def _inspect_text(payload: bytes, relative: str) -> None:
    path = Path(relative)
    if relative.startswith(("tests/fixtures/", "frontend/tests/fixtures/")) and path.suffix in {
        ".mp4",
        ".wav",
        ".bin",
    }:
        from scripts.security_audit import (
            PRIVATE_PATH_PATTERN,
            PRIVATE_SECRET_PATTERN,
            SIGNED_QUERY_PATTERN,
        )

        # CRITICAL: intentionally malformed media is a valid test fixture, but raw binary
        # bytes must still be screened for operator paths, credentials and signed URLs.
        binary_text = payload.decode("latin-1")
        if any(
            pattern.search(binary_text)
            for pattern in (
                PRIVATE_PATH_PATTERN,
                PRIVATE_SECRET_PATTERN,
                SIGNED_QUERY_PATTERN,
            )
        ):
            raise RegistryPayloadError("Registry test fixture exposes private content: " + relative)
        return
    if path.suffix.casefold() not in TEXT_SUFFIXES and path.name.casefold() not in TEXT_NAMES:
        return
    try:
        text = payload.decode("utf-8", errors="strict").casefold()
    except UnicodeDecodeError as exc:
        raise RegistryPayloadError(
            f"Registry archive public text is not strict UTF-8: {relative}"
        ) from exc
    code_literal = relative.startswith(
        ("tests/", "scripts/", "frontend/")
    ) and path.suffix.casefold() in {".py", ".ts", ".tsx", ".js", ".ps1", ".sh"}
    policy_literal = relative in {
        ".comfyignore",
        ".pre-commit-config.yaml",
        "frontend/.prettierignore",
        "tests/fixtures/m8_04_release_matrix.json",
    }
    # CRITICAL: negative fixtures and deny-list code name forbidden paths as literals.
    # Keep the exception source/type-specific; prose/private records must still fail.
    if not (code_literal or policy_literal) and any(
        marker in text for marker in FORBIDDEN_CONTENT_MARKERS
    ):
        raise RegistryPayloadError(
            f"Registry archive public text exposes an internal marker: {relative}"
        )


def _source_binding(
    relative: str, payload: bytes, tracked: frozenset[str], source_root: Path | None = None
) -> tuple[str, int]:
    if relative not in tracked:
        raise RegistryPayloadError(f"Registry archive member is not Git-tracked: {relative}")
    source = (ROOT if source_root is None else source_root).joinpath(*relative.split("/"))
    source_digest, source_size = _hash_regular_file(
        source,
        maximum_bytes=MAX_FILE_BYTES,
        label=f"Registry source {relative}",
    )
    member_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if source_size != len(payload) or source_digest != member_digest:
        raise RegistryPayloadError(
            f"Registry archive member does not match exact source bytes: {relative}"
        )
    return source_digest, source_size


def _archive_candidate(archive_path: Path, source_root: Path | None = None) -> Path:
    if not isinstance(archive_path, Path):
        raise RegistryPayloadError("Registry archive path must be a pathlib.Path")
    root = ROOT if source_root is None else source_root
    return archive_path if archive_path.is_absolute() else root / archive_path


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def build_registry_payload_report(
    archive_path: Path, *, source_root: Path | None = None
) -> JsonObject:
    """Audit one actual comfy-cli pack ZIP and bind every member to the exact source tree."""

    root = ROOT if source_root is None else validate_directory(source_root)
    candidate = _archive_candidate(archive_path, root)
    archive_digest, archive_size = _hash_regular_file(
        candidate,
        maximum_bytes=MAX_ARCHIVE_BYTES,
        label="Registry archive",
    )
    if archive_size == 0:
        raise RegistryPayloadError("Registry archive size is outside the bound")
    tracked = _git_tracked_paths(root)
    entries: list[JsonObject] = []
    seen: set[str] = set()
    expanded_total = 0
    try:
        with zipfile.ZipFile(candidate, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_FILES:
                raise RegistryPayloadError("Registry archive file count is outside the bound")
            admitted: list[tuple[zipfile.ZipInfo, str]] = []
            for info in infos:
                relative = _canonical_member_path(info)
                collision_key = relative.casefold()
                if collision_key in seen:
                    raise RegistryPayloadError(
                        "Registry archive contains a normalized path collision or duplicate"
                    )
                seen.add(collision_key)
                _validate_member_metadata(info, relative)
                _validate_member_policy(relative)
                expanded_total += info.file_size
                if expanded_total > MAX_TOTAL_BYTES:
                    raise RegistryPayloadError(
                        "Registry archive expanded size is outside the bound"
                    )
                admitted.append((info, relative))
            for info, relative in admitted:
                payload = _read_member(archive, info, relative)
                _inspect_text(payload, relative)
                source_digest, source_size = _source_binding(relative, payload, tracked, root)
                member_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                entries.append(
                    {
                        "compressed_size": info.compress_size,
                        "path": relative,
                        "sha256": member_digest,
                        "size": source_size,
                        "source_sha256": source_digest,
                    }
                )
    except RegistryPayloadError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise RegistryPayloadError(
            "Registry ZIP archive is missing, corrupt or unsupported"
        ) from exc
    final_digest, final_size = _hash_regular_file(
        candidate,
        maximum_bytes=MAX_ARCHIVE_BYTES,
        label="Registry archive",
    )
    if final_digest != archive_digest or final_size != archive_size:
        raise RegistryPayloadError("Registry archive changed during audit")
    entries.sort(key=lambda entry: str(entry["path"]))
    if sum(entry["path"] == RUNTIME_MEMBER for entry in entries) != 1:
        raise RegistryPayloadError("Registry archive must contain exactly one runtime bundle")
    try:
        required = checkout_required_paths(root, tracked)
        require_present(required, (str(entry["path"]) for entry in entries), phase="Registry ZIP")
    except (CompletenessError, OSError) as exc:
        raise RegistryPayloadError(str(exc)) from exc
    return {
        "archive_sha256": archive_digest,
        "archive_size": archive_size,
        "entries": entries,
        "file_count": len(entries),
        "schema": "h3-context-registry-payload/2",
        "source": "comfy-cli-pack-archive",
        "status": "PASS",
        "total_bytes": expanded_total,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args(argv)
    try:
        report_path: Path | None = None
        if args.report is not None:
            report_path = prepare_regular_output(args.report)
            archive_file = validate_regular_file(
                _archive_candidate(args.archive, args.source_root), maximum_bytes=MAX_ARCHIVE_BYTES
            )
            if _paths_alias(archive_file, report_path):
                raise RegistryPayloadError("Registry report path aliases the audited archive")
        report = build_registry_payload_report(args.archive, source_root=args.source_root)
        root = ROOT if args.source_root is None else validate_directory(args.source_root)
        smoke_archive(_archive_candidate(args.archive, root), root, report)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if report_path is not None:
            report_path.write_text(encoded + "\n", encoding="utf-8")
    except (
        EOFError,
        OSError,
        RegistryPayloadError,
        CompletenessError,
        UnicodeDecodeError,
        UnsafePathError,
        zlib.error,
    ) as exc:
        print(f"REGISTRY PAYLOAD: FAIL: {exc}")
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
