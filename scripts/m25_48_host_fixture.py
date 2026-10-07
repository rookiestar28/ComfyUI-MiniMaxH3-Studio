"""Explicitly prepare or remove the public M25-48 supplied-host media fixture.

This helper is a separately selected maintenance operation. It never discovers a host, starts a
process, installs dependencies, changes a candidate, or runs a browser row. Its output contains
only the relative ComfyUI output locator and fixed public fixture facts.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path
from typing import NoReturn

FIXTURE_SHA256 = "f05b737bbb9fe8bcc0fad771184f78191b8059baa56403ce18ab13d3c86d81f6"  # noqa: E501  # pragma: allowlist secret
FIXTURE_BYTES = 5_749
FIXTURE_SUBFOLDER = "h3-context-m25-48-fixtures"
FIXTURE_SOURCE = Path(__file__).resolve().parent / "fixtures" / "m25_48_192_frame_mp4.b64"
_ATTEMPT = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_FILENAME = re.compile(r"h3-m25-48-[a-z0-9][a-z0-9-]{0,47}\.mp4\Z")


class FixtureError(RuntimeError):
    """The explicit fixture maintenance request failed admission."""


def _fail(message: str) -> NoReturn:
    raise FixtureError(message)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture_bytes() -> bytes:
    try:
        encoded = "".join(FIXTURE_SOURCE.read_text(encoding="ascii").split())
        payload = base64.b64decode(encoded, validate=True)
    except (OSError, UnicodeError, binascii.Error) as error:
        raise FixtureError("fixture source is unavailable or malformed") from error
    if len(payload) != FIXTURE_BYTES or _digest(payload) != FIXTURE_SHA256:
        _fail("fixture source identity mismatch")
    return payload


def _root(value: str | Path) -> Path:
    root = Path(value).resolve(strict=True)
    if not root.is_dir():
        _fail("host output root is not a directory")
    return root


def _has_link(path: Path, stop: Path) -> bool:
    current = path
    while current != stop:
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(is_junction) and is_junction()):
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def _target(root_value: str | Path, filename: str) -> tuple[Path, Path]:
    root = _root(root_value)
    folder = root / FIXTURE_SUBFOLDER
    folder.mkdir(mode=0o700, exist_ok=True)
    if _has_link(folder, root):
        _fail("fixture destination contains a link or junction")
    resolved_folder = folder.resolve(strict=True)
    if os.path.commonpath((str(root), str(resolved_folder))) != str(root):
        _fail("fixture destination escapes the host output root")
    target = resolved_folder / filename
    return resolved_folder, target


def _locator(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise FixtureError("fixture locator is malformed") from error
    if (
        type(parsed) is not dict
        or set(parsed) != {"filename", "subfolder", "type"}
        or type(parsed.get("filename")) is not str
        or _FILENAME.fullmatch(parsed["filename"]) is None
        or parsed.get("subfolder") != FIXTURE_SUBFOLDER
        or parsed.get("type") != "output"
    ):
        _fail("fixture locator is not admitted")
    return parsed


def prepare(root_value: str | Path, attempt: str) -> dict[str, object]:
    if _ATTEMPT.fullmatch(attempt) is None:
        _fail("fixture attempt is invalid")
    filename = f"h3-m25-48-{attempt}.mp4"
    _folder, target = _target(root_value, filename)
    if target.exists():
        _fail("fixture target already exists")
    payload = _fixture_bytes()
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists():
        _fail("fixture temporary target already exists")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if target.stat().st_size != FIXTURE_BYTES or _digest(target.read_bytes()) != FIXTURE_SHA256:
        target.unlink(missing_ok=True)
        _fail("fixture target identity mismatch after replacement")
    return {
        "status": "prepared",
        "locator": {
            "filename": filename,
            "subfolder": FIXTURE_SUBFOLDER,
            "type": "output",
        },
        "bytes": FIXTURE_BYTES,
        "sha256": FIXTURE_SHA256,
        "frames": 192,
        "width": 64,
        "height": 64,
        "frame_rate": "24/1",
    }


def cleanup(root_value: str | Path, locator_json: str) -> dict[str, object]:
    locator = _locator(locator_json)
    folder, target = _target(root_value, locator["filename"])
    if not target.is_file() or target.is_symlink():
        _fail("fixture target is missing or not a regular file")
    payload = target.read_bytes()
    if len(payload) != FIXTURE_BYTES or _digest(payload) != FIXTURE_SHA256:
        _fail("fixture target digest mismatch; refusing removal")
    target.unlink()
    try:
        folder.rmdir()
    except OSError:
        pass
    return {"status": "removed", "sha256": FIXTURE_SHA256}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--host-output-root", required=True)
    prepare_parser.add_argument("--attempt", required=True)
    cleanup_parser = commands.add_parser("cleanup")
    cleanup_parser.add_argument("--host-output-root", required=True)
    cleanup_parser.add_argument("--locator-json", required=True)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "prepare":
            result = prepare(arguments.host_output_root, arguments.attempt)
        else:
            result = cleanup(arguments.host_output_root, arguments.locator_json)
    except (FixtureError, OSError) as error:
        print(json.dumps({"status": "failed", "reason": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
