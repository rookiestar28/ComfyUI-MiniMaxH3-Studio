"""Fail-closed regular-file and directory component validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a filesystem path crosses an unsafe component."""


def _absolute_lexical(path: Path) -> Path:
    if not isinstance(path, Path):
        raise UnsafePathError("path must be a pathlib.Path")
    candidate = path if path.is_absolute() else Path.cwd() / path
    if any(part in {"", ".", ".."} for part in candidate.parts[1:]):
        raise UnsafePathError("path contains an unsafe lexical component")
    return candidate


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        path.is_symlink()
        or stat.S_ISLNK(metadata.st_mode)
        or (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


def validate_path_components(path: Path) -> tuple[Path, os.stat_result]:
    """Validate every existing component without following links or reparse points."""

    candidate = _absolute_lexical(path)
    current = Path(candidate.anchor)
    metadata: os.stat_result | None = None
    for part in candidate.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise UnsafePathError("path component is missing or unavailable") from exc
        # CRITICAL: never replace lstat/reparse checks with resolving or leaf-only inspection.
        if _is_link_or_reparse(current, metadata):
            raise UnsafePathError("path contains a link or reparse component")
    if metadata is None:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise UnsafePathError("path is missing or unavailable") from exc
        if _is_link_or_reparse(current, metadata):
            raise UnsafePathError("path contains a link or reparse component")
    return candidate, metadata


def validate_regular_file(path: Path, *, maximum_bytes: int | None = None) -> Path:
    """Return a lexically absolute safe regular file after optional size admission."""

    candidate, metadata = validate_path_components(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError("path is not a regular file")
    if maximum_bytes is not None and (
        maximum_bytes <= 0 or metadata.st_size < 0 or metadata.st_size > maximum_bytes
    ):
        raise UnsafePathError("regular file size is outside the bound")
    return candidate


def read_regular_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one admitted regular file with an independent bounded-read check."""

    candidate = validate_regular_file(path, maximum_bytes=maximum_bytes)
    try:
        with candidate.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as exc:
        raise UnsafePathError("regular file cannot be read") from exc
    if not payload or len(payload) > maximum_bytes:
        raise UnsafePathError("regular file size is outside the bound")
    return payload


def validate_directory(path: Path) -> Path:
    """Return a lexically absolute safe existing directory."""

    candidate, metadata = validate_path_components(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafePathError("path is not a directory")
    return candidate


def ensure_directory(path: Path) -> Path:
    """Create missing components one at a time without crossing existing links/reparse points."""

    candidate = _absolute_lexical(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
                metadata = current.lstat()
            except OSError as exc:
                raise UnsafePathError("directory component cannot be created") from exc
        except OSError as exc:
            raise UnsafePathError("directory component is unavailable") from exc
        # CRITICAL: creation must re-check the resulting entry to close link/reparse races.
        if _is_link_or_reparse(current, metadata):
            raise UnsafePathError("path contains a link or reparse component")
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafePathError("path component is not a directory")
    return candidate


def prepare_regular_output(path: Path) -> Path:
    """Return a safe output path whose parent is safe and whose existing leaf is regular."""

    if path.name in {"", ".", ".."}:
        raise UnsafePathError("output filename is unsafe")
    parent = ensure_directory(path.parent)
    target = parent / path.name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise UnsafePathError("output file is unavailable") from exc
    # CRITICAL: an existing report target must never redirect writes through a link/reparse point.
    if _is_link_or_reparse(target, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError("output file contains a link or reparse component")
    return target


__all__ = [
    "UnsafePathError",
    "ensure_directory",
    "prepare_regular_output",
    "read_regular_file_bytes",
    "validate_directory",
    "validate_path_components",
    "validate_regular_file",
]
