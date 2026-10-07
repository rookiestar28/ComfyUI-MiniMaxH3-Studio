"""PEP 517 setuptools adapter with an explicit, bounded public source snapshot.

Only stdlib and the declared build dependency may be imported here. Runtime modules and
developer tooling must remain independent of isolated build environments (Python 3.10+).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from setuptools import build_meta

from scripts.public_source_policy import allowed

INVENTORY = "public-source-inventory.json"
SCHEMA = "h3-public-source-inventory/1"
MAX_FILES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
RESERVED = {"aux", "clock$", "con", "nul", "prn"} | {
    f"{prefix}{index}" for prefix in ("com", "lpt") for index in range(1, 10)
}


class PublicBuildError(ValueError):
    """The source cannot safely supply a public Python distribution."""


def canonical(path: str) -> str:
    parts = path.split("/")
    if (
        not path
        or len(path) > 1024
        or unicodedata.normalize("NFC", path) != path
        or "\\" in path
        or ":" in path
        or any(not c.isprintable() for c in path)
        or any(
            p in {"", ".", ".."}
            or len(p) > 255
            or p.endswith((".", " "))
            or p.split(".", 1)[0].casefold() in RESERVED
            for p in parts
        )
    ):
        raise PublicBuildError("public source path is unsafe or non-canonical")
    return path


def _no_links(path: Path) -> os.stat_result:
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PublicBuildError("public source contains a link or reparse component")
    return path.lstat()


def _read(root: Path, relative: str) -> bytes:
    path = root / canonical(relative)
    try:
        info = _no_links(path)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_FILE_BYTES:
            raise PublicBuildError("public source must be a bounded regular file")
        with path.open("rb") as stream:
            payload = stream.read(MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise PublicBuildError("public source file exceeds the size bound")
        return payload
    except OSError as exc:
        raise PublicBuildError("public source input is missing or unreadable") from exc


def _git(root: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=data,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicBuildError("Git is unavailable; build from an issued sdist inventory") from exc


def _temporary_parent(root: Path) -> Path:
    _no_links(root)
    path = root / ".tmp"
    path.mkdir(exist_ok=True)
    if not stat.S_ISDIR(_no_links(path).st_mode):
        raise PublicBuildError("build temporary parent must be a regular directory")
    return path


def _check_ignored(root: Path, paths: list[str], ignores: dict[str, bytes]) -> None:
    # IMPORTANT: use candidate ignore bytes in a fresh Git policy tree. Local excludes,
    # parent repositories and global Git settings cannot redefine the public candidate.
    with tempfile.TemporaryDirectory(prefix="public-ignore-", dir=_temporary_parent(root)) as tmp:
        policy = Path(tmp)
        for name, payload in ignores.items():
            target = policy / canonical(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if _git(policy, "init", "-q").returncode:
            raise PublicBuildError("candidate ignore policy cannot be initialized")
        empty = policy / ".git" / "empty-excludes"
        empty.write_bytes(b"")
        result = _git(
            policy,
            "-c",
            "core.excludesFile=" + str(empty),
            "check-ignore",
            "--no-index",
            "--stdin",
            "-z",
            data="\0".join(paths).encode() + b"\0",
        )
        if result.returncode not in {0, 1}:
            raise PublicBuildError("candidate ignore policy cannot be evaluated")
        if result.stdout:
            raise PublicBuildError("Git-ignored input cannot enter a public build")


def _inventory_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicBuildError("issued source inventory contains a duplicate member")
        result[key] = value
    return result


def _issued(root: Path) -> dict[str, str]:
    try:
        value = json.loads(_read(root, INVENTORY), object_pairs_hook=_inventory_pairs)
    except (OSError, ValueError) as exc:
        raise PublicBuildError(
            "source requires an exact Git root or valid issued sdist inventory; "
            "use an issued sdist, or initialize and track a reviewed public checkout"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "files"}
        or value["schema"] != SCHEMA
    ):
        raise PublicBuildError("issued source inventory has an invalid schema")
    rows = value["files"]
    if not isinstance(rows, dict) or not 0 < len(rows) <= MAX_FILES:
        raise PublicBuildError("issued source inventory has an invalid file count")
    for path, digest in rows.items():
        canonical(path)
        if (
            not allowed(path)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PublicBuildError("issued source inventory contains an invalid public entry")
    return {path: str(digest) for path, digest in rows.items()}


def public_sources(root: Path) -> dict[str, bytes]:
    """Select current tracked bytes, or verify only the paths in an issued inventory."""
    root = root.absolute()
    _no_links(root)
    top = _git(root, "rev-parse", "--show-toplevel") if (root / ".git").exists() else None
    issued: dict[str, str] | None = None
    if (
        top is not None
        and top.returncode == 0
        and os.path.normcase(os.path.abspath(top.stdout.decode("utf-8").strip()))
        == os.path.normcase(str(root))
    ):
        result = _git(root, "ls-files", "--stage", "-z")
        if result.returncode:
            raise PublicBuildError("public Git inventory is unavailable")
        paths = []
        ignores: dict[str, bytes] = {}
        for row in result.stdout.decode("utf-8").split("\0"):
            if not row:
                continue
            metadata, path = row.split("\t", 1)
            mode, _oid, stage = metadata.split()
            canonical(path)
            if not allowed(path):
                continue
            if mode not in {"100644", "100755"} or stage != "0":
                raise PublicBuildError("public Git inventory requires unconflicted regular files")
            paths.append(path)
            if Path(path).name == ".gitignore":
                ignores[path] = _read(root, path)
        _check_ignored(root, paths, ignores)
    else:
        issued = _issued(root)
        paths = list(issued)
    if not paths or len(paths) > MAX_FILES or len({p.casefold() for p in paths}) != len(paths):
        raise PublicBuildError("public source inventory is empty, excessive or colliding")
    payloads = {}
    total = 0
    for path in sorted(paths):
        payload = _read(root, path)
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            raise PublicBuildError("public source exceeds the total size bound")
        if issued is not None and hashlib.sha256(payload).hexdigest() != issued[path]:
            raise PublicBuildError("issued source input differs from its recorded hash")
        payloads[path] = payload
    # An issued inventory freezes the issuing build's ignore decision and exact bytes.
    # Do not rediscover files (or require Git) while rebuilding that Git-less sdist.
    if not {"pyproject.toml", "MANIFEST.in", ".gitignore"} <= payloads.keys():
        raise PublicBuildError("public source lacks required build metadata or ignore policy")
    return payloads


def inventory_bytes(payloads: dict[str, bytes]) -> bytes:
    return (
        json.dumps(
            {
                "schema": SCHEMA,
                "files": {
                    path: hashlib.sha256(payload).hexdigest()
                    for path, payload in sorted(payloads.items())
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


@contextmanager
def _staged() -> Iterator[None]:
    root = Path.cwd().absolute()
    payloads = public_sources(root)
    # CRITICAL: never delegate a non-editable build in the raw checkout. MANIFEST grafts
    # and stale SOURCES.txt otherwise admit ignored private records and untracked inputs.
    with tempfile.TemporaryDirectory(prefix="public-build-", dir=_temporary_parent(root)) as tmp:
        stage = Path(tmp)
        for path, payload in payloads.items():
            target = stage / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        (stage / INVENTORY).write_bytes(inventory_bytes(payloads))
        os.chdir(stage)
        try:
            yield
        finally:
            os.chdir(root)


def get_requires_for_build_sdist(config_settings: Any = None) -> list[str]:
    with _staged():
        return list(build_meta.get_requires_for_build_sdist(config_settings))


def get_requires_for_build_wheel(config_settings: Any = None) -> list[str]:
    with _staged():
        return list(build_meta.get_requires_for_build_wheel(config_settings))


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings: Any = None) -> str:
    destination = str(Path(metadata_directory).absolute())
    with _staged():
        return str(build_meta.prepare_metadata_for_build_wheel(destination, config_settings))


def build_sdist(sdist_directory: str, config_settings: Any = None) -> str:
    destination = str(Path(sdist_directory).absolute())
    with _staged():
        return str(build_meta.build_sdist(destination, config_settings))


def build_wheel(
    wheel_directory: str, config_settings: Any = None, metadata_directory: str | None = None
) -> str:
    destination = str(Path(wheel_directory).absolute())
    metadata = None if metadata_directory is None else str(Path(metadata_directory).absolute())
    with _staged():
        return str(build_meta.build_wheel(destination, config_settings, metadata))


# Editable hooks must retain the real source path; a temporary snapshot would break imports
# as soon as pip finishes. Ordinary wheels and sdists always use the sanitized hooks above.
get_requires_for_build_editable = build_meta.get_requires_for_build_editable
prepare_metadata_for_build_editable = build_meta.prepare_metadata_for_build_editable
build_editable = build_meta.build_editable
