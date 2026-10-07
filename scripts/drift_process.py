"""Classify one local content-free drift event without fetching, executing, or publishing."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.drift_process import (  # noqa: E402
    MAX_DRIFT_EVENT_BYTES,
    DriftEventError,
    classify_drift_event,
    decode_drift_event_json,
)

ERROR_SCHEMA = "h3.drift.process.error.v1"
_WINDOWS_REPARSE_ATTRIBUTE = 0x400


class DriftProcessCliError(ValueError):
    """Raised when the exact event entry cannot be read safely."""


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _require_nonlink_components(path: Path) -> None:
    # CRITICAL: event inputs must never traverse symlinks or Windows reparse points.
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_link_or_reparse(current):
            raise DriftProcessCliError("event path contains a link or reparse component")


def _require_local_filesystem_namespace(path: Path) -> None:
    """Reject Windows network/device namespaces before any filesystem-shaped operation."""

    raw = str(path).replace("/", "\\")
    folded = raw.casefold()
    if folded.startswith("\\\\") or folded.startswith("\\??\\"):
        raise DriftProcessCliError("event path must use the local filesystem namespace")


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def read_event_file(path: Path) -> bytes:
    """Read one regular event exactly once while detecting replacement or mutation races."""

    if not isinstance(path, Path):
        raise DriftProcessCliError("event path must be a Path")
    # CRITICAL: reject network/device namespaces before any filesystem-shaped operation.
    _require_local_filesystem_namespace(path)
    _require_nonlink_components(path)
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DriftProcessCliError("event path is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DRIFT_EVENT_BYTES:
        raise DriftProcessCliError("event path is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DriftProcessCliError("event path cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise DriftProcessCliError("event changed before read")
        chunks: list[bytes] = []
        remaining = MAX_DRIFT_EVENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > MAX_DRIFT_EVENT_BYTES:
            raise DriftProcessCliError("event exceeds the byte limit")
        if _identity(after) != _identity(opened) or len(payload) != opened.st_size:
            raise DriftProcessCliError("event changed during read")
    except OSError as exc:
        raise DriftProcessCliError("event read failed closed") from exc
    finally:
        os.close(descriptor)
    try:
        final = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DriftProcessCliError("event changed after read") from exc
    if _identity(final) != _identity(opened) or _is_link_or_reparse(path):
        raise DriftProcessCliError("event changed after read")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", type=Path, help="exact local content-free drift event JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        event = decode_drift_event_json(read_event_file(args.event))
        report = classify_drift_event(event)
    except (DriftProcessCliError, DriftEventError, OSError):
        print(json.dumps({"schema": ERROR_SCHEMA, "status": "INVALID_INPUT"}, sort_keys=True))
        return 1
    print(json.dumps(report.to_wire(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 2 if report.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
