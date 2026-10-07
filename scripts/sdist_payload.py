"""Audit the actual standard Python sdist against independent source requirements."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

import tomli

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import product_completeness, public_build_backend, registry_payload  # noqa: E402


class SdistPayloadError(ValueError):
    """The source archive is unsafe, incomplete or differs from its candidate."""


def audited_members(archive_path: Path, source_root: Path) -> dict[str, bytes]:
    """Validate before exposing bytes; never use tarfile's filesystem extraction API."""
    try:
        expected = public_build_backend.public_sources(source_root)
        try:
            upstream = registry_payload._git_tracked_paths(source_root)
        except registry_payload.RegistryPayloadError:
            upstream = frozenset(public_build_backend._issued(source_root))
        required = product_completeness.required_paths(
            upstream, lambda path: public_build_backend._read(source_root, path)
        )
        project = tomli.loads(expected["pyproject.toml"].decode())["project"]
        name = re.sub(r"[-_.]+", "_", project["name"]).lower()
        prefix = name + "-" + project["version"]
        metadata = {
            "PKG-INFO",
            "setup.cfg",
            public_build_backend.INVENTORY,
            *(
                name + ".egg-info/" + item
                for item in (
                    "PKG-INFO",
                    "SOURCES.txt",
                    "dependency_links.txt",
                    "top_level.txt",
                    "requires.txt",
                )
            ),
        }
        registry_payload._hash_regular_file(
            archive_path, maximum_bytes=registry_payload.MAX_ARCHIVE_BYTES, label="sdist"
        )
        # Check the entire gzip stream (including its trailer), with an expansion bound.
        with gzip.open(archive_path, "rb") as stream:
            expanded = 0
            limit = public_build_backend.MAX_TOTAL_BYTES + public_build_backend.MAX_FILES * 2048
            while chunk := stream.read(min(1024 * 1024, limit + 1 - expanded)):
                expanded += len(chunk)
                if expanded > limit:
                    raise SdistPayloadError("sdist stream exceeds the expansion bound")
        members: dict[str, bytes] = {}
        seen: set[str] = set()
        total = 0
        with tarfile.open(archive_path, "r:gz") as archive:
            count = 0
            for info in archive:
                count += 1
                if count > public_build_backend.MAX_FILES * 2:
                    raise SdistPayloadError("sdist member count exceeds the bound")
                full = registry_payload._canonical_relative_path(info.name, label="sdist member")
                key = full.casefold()
                if key in seen:
                    raise SdistPayloadError("sdist contains a duplicate or normalized collision")
                seen.add(key)
                parts = full.split("/", 1)
                if parts[0] != prefix or (len(parts) == 1 and not info.isdir()):
                    raise SdistPayloadError("sdist has an unexpected source root")
                if info.isdir():
                    if info.size != 0:
                        raise SdistPayloadError("sdist directory carries unexpected data")
                    # Directories must be ancestors of admitted source or generated metadata.
                    if len(parts) == 2 and not any(
                        path.startswith(parts[1] + "/") for path in expected.keys() | metadata
                    ):
                        raise SdistPayloadError("sdist contains an unexpected directory")
                    continue
                if not info.isfile() or info.issparse() or info.linkname:
                    raise SdistPayloadError("sdist contains a link, sparse or special member")
                relative = parts[1]
                if relative not in expected and relative not in metadata:
                    raise SdistPayloadError("sdist contains an unexpected or private member")
                if not 0 <= info.size <= public_build_backend.MAX_FILE_BYTES:
                    raise SdistPayloadError("sdist file exceeds the size bound")
                total += info.size
                if total > public_build_backend.MAX_TOTAL_BYTES:
                    raise SdistPayloadError("sdist payload exceeds the total size bound")
                reader = archive.extractfile(info)
                if reader is None:
                    raise SdistPayloadError("sdist member is unavailable")
                with reader:
                    payload = reader.read(public_build_backend.MAX_FILE_BYTES + 1)
                if len(payload) != info.size:
                    raise SdistPayloadError("sdist member is corrupt or truncated")
                registry_payload._inspect_text(
                    payload, relative if relative in expected else "sdist-metadata.txt"
                )
                if relative in expected and payload != expected[relative]:
                    raise SdistPayloadError("sdist member differs from exact public source bytes")
                members[relative] = payload
        product_completeness.require_present(required, members, phase="sdist")
        product_completeness.require_present(expected, members, phase="public source snapshot")
        if members.get(public_build_backend.INVENTORY) != public_build_backend.inventory_bytes(
            expected
        ):
            raise SdistPayloadError("sdist public inventory differs from source requirements")
        sources = members.get(name + ".egg-info/SOURCES.txt", b"").decode().splitlines()
        if not sources or any(path not in expected and path not in metadata for path in sources):
            raise SdistPayloadError("sdist metadata references an unexpected or private source")
        if not {"PKG-INFO", "setup.cfg", name + ".egg-info/PKG-INFO"} <= members.keys():
            raise SdistPayloadError("sdist lacks generated package metadata")
        return members
    except (
        OSError,
        ValueError,
        EOFError,
        tarfile.TarError,
        registry_payload.RegistryPayloadError,
    ) as exc:
        if isinstance(exc, SdistPayloadError):
            raise
        raise SdistPayloadError(str(exc)) from exc


def build_sdist_payload_report(archive_path: Path, *, source_root: Path = ROOT) -> dict[str, Any]:
    before = registry_payload._hash_regular_file(
        archive_path, maximum_bytes=registry_payload.MAX_ARCHIVE_BYTES, label="sdist"
    )
    payloads = audited_members(archive_path, source_root)
    after = registry_payload._hash_regular_file(
        archive_path, maximum_bytes=registry_payload.MAX_ARCHIVE_BYTES, label="sdist"
    )
    if before != after:
        raise SdistPayloadError("sdist changed during audit")
    return {
        "schema": "h3-context-sdist-payload/1",
        "status": "PASS",
        "archive_sha256": before[0],
        "file_count": len(payloads),
        "entries": [
            {"path": p, "size": len(b), "sha256": "sha256:" + hashlib.sha256(b).hexdigest()}
            for p, b in sorted(payloads.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--source-root", default=ROOT, type=Path)
    args = parser.parse_args()
    try:
        report = build_sdist_payload_report(args.archive, source_root=args.source_root)
    except SdistPayloadError:
        print("Python sdist audit: FAIL")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
