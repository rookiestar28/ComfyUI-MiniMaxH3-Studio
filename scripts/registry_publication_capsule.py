"""Create and safely unpack the secret-free Registry publication capsule."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    prepare_regular_output,
    validate_directory,
    validate_path_components,
    validate_regular_file,
)

MAX_CAPSULE_BYTES = 384 * 1024 * 1024
MAX_FILES = 20_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_NAME_CHARS = 1_024
MAX_COMPONENT_CHARS = 255
READ_CHUNK_BYTES = 1024 * 1024
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENVIRONMENT_ROOT = ".publish-venv"
ARCHIVE_MEMBER = "node.zip"
REPORT_MEMBER = "registry-payload.json"
CANDIDATE_MEMBER = "publication-candidate.txt"
FIXED_FILE_MEMBERS = frozenset({ARCHIVE_MEMBER, REPORT_MEMBER, CANDIDATE_MEMBER})

JsonObject = dict[str, Any]


class PublicationCapsuleError(RuntimeError):
    """Raised when a publication capsule is unsafe, incomplete or ambiguous."""


def compute_capsule_sha256(capsule: Path) -> str:
    """Hash the entire bounded capsule transferred across the artifact boundary."""

    try:
        capsule_file = validate_regular_file(capsule, maximum_bytes=MAX_CAPSULE_BYTES)
        digest = hashlib.sha256()
        with capsule_file.open("rb") as stream:
            while chunk := stream.read(READ_CHUNK_BYTES):
                digest.update(chunk)
    except UnsafePathError as exc:
        raise PublicationCapsuleError("publication capsule path is unsafe") from exc
    except OSError as exc:
        raise PublicationCapsuleError("publication capsule cannot be hashed") from exc
    return digest.hexdigest()


def verify_capsule_sha256(capsule: Path, expected_sha256: str) -> str:
    """Fail closed unless the downloaded capsule matches the preflight digest."""

    if SHA256.fullmatch(expected_sha256) is None:
        raise PublicationCapsuleError(
            "expected capsule SHA-256 must be 64 lowercase hex characters"
        )
    observed = compute_capsule_sha256(capsule)
    if not hmac.compare_digest(observed, expected_sha256):
        raise PublicationCapsuleError("publication capsule digest does not match preflight")
    return observed


def _candidate(value: str, *, label: str) -> str:
    if FULL_SHA.fullmatch(value) is None or value == "0" * 40:
        raise PublicationCapsuleError(f"{label} must be one non-zero full Git SHA")
    return value


def _canonical_member_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not value
        or normalized != value
        or len(value) > MAX_NAME_CHARS
        or "\\" in value
        or ":" in value
        or value.startswith("/")
        or any(not character.isprintable() for character in value)
    ):
        raise PublicationCapsuleError("publication capsule member name is unsafe")
    # SECURITY: reject non-canonical separators before PurePosixPath can collapse them.
    raw_parts = value.split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise PublicationCapsuleError("publication capsule member path is unsafe")
    if any(len(part) > MAX_COMPONENT_CHARS for part in raw_parts):
        raise PublicationCapsuleError("publication capsule member name is outside the bound")
    if value not in FIXED_FILE_MEMBERS and not (
        value == ENVIRONMENT_ROOT or value.startswith(ENVIRONMENT_ROOT + "/")
    ):
        raise PublicationCapsuleError("publication capsule contains an unexpected member")
    return normalized


def _link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        path.is_symlink()
        or stat.S_ISLNK(metadata.st_mode)
        or (attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _environment_members(environment: Path) -> tuple[tuple[Path, str, bool, int], ...]:
    safe_environment = validate_directory(environment)
    members: list[tuple[Path, str, bool, int]] = [(safe_environment, ENVIRONMENT_ROOT, True, 0)]
    total_bytes = 0
    for current_text, directory_names, file_names in os.walk(
        safe_environment, topdown=True, followlinks=False
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(current_text)
        for name, is_directory in (
            *((name, True) for name in directory_names),
            *((name, False) for name in file_names),
        ):
            source = current / name
            try:
                safe_source, metadata = validate_path_components(source)
            except UnsafePathError as exc:
                raise PublicationCapsuleError(
                    "publication environment contains an unsafe path"
                ) from exc
            if _link_or_reparse(safe_source, metadata):
                raise PublicationCapsuleError(
                    "publication environment contains a link or reparse point"
                )
            relative = safe_source.relative_to(safe_environment).as_posix()
            member_name = _canonical_member_name(f"{ENVIRONMENT_ROOT}/{relative}")
            if is_directory:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PublicationCapsuleError("publication environment entry type changed")
                size = 0
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise PublicationCapsuleError(
                        "publication environment contains a non-regular file"
                    )
                size = metadata.st_size
                if size < 0 or size > MAX_MEMBER_BYTES:
                    raise PublicationCapsuleError(
                        "publication capsule member size is outside the bound"
                    )
                total_bytes += size
            members.append((safe_source, member_name, is_directory, size))
            if len(members) + len(FIXED_FILE_MEMBERS) > MAX_FILES:
                raise PublicationCapsuleError("publication capsule file count is outside the bound")
            if total_bytes > MAX_TOTAL_BYTES:
                raise PublicationCapsuleError(
                    "publication capsule expanded size is outside the bound"
                )
    return tuple(members)


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    if info.isdir():
        info.mode = 0o755
    elif info.mode & 0o111:
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def create_capsule(
    output: Path,
    *,
    environment: Path,
    archive: Path,
    report: Path,
    candidate: str,
) -> JsonObject:
    """Create one bounded, normalized capsule without following environment links."""

    candidate = _candidate(candidate, label="candidate commit")
    try:
        target = prepare_regular_output(output)
        if target.exists():
            raise PublicationCapsuleError("publication capsule output already exists")
        archive_file = validate_regular_file(archive, maximum_bytes=MAX_MEMBER_BYTES)
        report_file = validate_regular_file(report, maximum_bytes=MAX_MEMBER_BYTES)
        environment_members = _environment_members(environment)
    except UnsafePathError as exc:
        raise PublicationCapsuleError("publication capsule input or output path is unsafe") from exc
    candidate_payload = (candidate + "\n").encode("ascii")
    fixed_size = archive_file.stat().st_size + report_file.stat().st_size + len(candidate_payload)
    environment_size = sum(item[3] for item in environment_members)
    if fixed_size + environment_size > MAX_TOTAL_BYTES:
        raise PublicationCapsuleError("publication capsule expanded size is outside the bound")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="registry-publication-", suffix=".tgz", dir=target.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        with tarfile.open(
            temporary_path,
            "w:gz",
            format=tarfile.PAX_FORMAT,
            dereference=False,
        ) as stream:
            for source, member_name, _is_directory, _size in environment_members:
                stream.add(
                    source,
                    arcname=member_name,
                    recursive=False,
                    filter=_normalized_tar_info,
                )
            stream.add(
                archive_file,
                arcname=ARCHIVE_MEMBER,
                recursive=False,
                filter=_normalized_tar_info,
            )
            stream.add(
                report_file,
                arcname=REPORT_MEMBER,
                recursive=False,
                filter=_normalized_tar_info,
            )
            candidate_info = tarfile.TarInfo(CANDIDATE_MEMBER)
            candidate_info.size = len(candidate_payload)
            candidate_info.mode = 0o644
            stream.addfile(_normalized_tar_info(candidate_info), io.BytesIO(candidate_payload))
        if temporary_path.stat().st_size <= 0 or temporary_path.stat().st_size > MAX_CAPSULE_BYTES:
            raise PublicationCapsuleError("publication capsule size is outside the bound")
        os.replace(temporary_path, target)
        temporary_path = None
    except (OSError, tarfile.TarError) as exc:
        raise PublicationCapsuleError("publication capsule cannot be created") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {
        "candidate_commit": candidate,
        "file_count": len(environment_members) + len(FIXED_FILE_MEMBERS),
        "schema": "h3-context-registry-publication-capsule/1",
        "status": "PASS",
        "total_bytes": fixed_size + environment_size,
    }


def _validated_members(
    stream: tarfile.TarFile,
    *,
    expected_candidate: str,
) -> tuple[tuple[tarfile.TarInfo, str], ...]:
    expected_candidate = _candidate(expected_candidate, label="expected candidate")
    members = stream.getmembers()
    if not members or len(members) > MAX_FILES:
        raise PublicationCapsuleError("publication capsule file count is outside the bound")
    seen: set[str] = set()
    admitted: list[tuple[tarfile.TarInfo, str]] = []
    expanded_total = 0
    for info in members:
        name = _canonical_member_name(info.name)
        collision_key = name.casefold()
        if collision_key in seen:
            raise PublicationCapsuleError("publication capsule contains a duplicate member")
        seen.add(collision_key)
        if not (info.isfile() or info.isdir()):
            raise PublicationCapsuleError("publication capsule contains a link or special member")
        if info.size < 0 or info.size > MAX_MEMBER_BYTES:
            raise PublicationCapsuleError("publication capsule member size is outside the bound")
        expanded_total += info.size
        if expanded_total > MAX_TOTAL_BYTES:
            raise PublicationCapsuleError("publication capsule expanded size is outside the bound")
        admitted.append((info, name))
    required = {ENVIRONMENT_ROOT, *FIXED_FILE_MEMBERS}
    if not required.issubset(seen):
        raise PublicationCapsuleError("publication capsule is incomplete")
    admitted_by_name = {name: info for info, name in admitted}
    if not admitted_by_name[ENVIRONMENT_ROOT].isdir():
        raise PublicationCapsuleError("publication environment root is not a directory")
    if any(not admitted_by_name[name].isfile() for name in FIXED_FILE_MEMBERS):
        raise PublicationCapsuleError("publication capsule payload is not a regular file")
    candidate_info = next(info for info, name in admitted if name == CANDIDATE_MEMBER)
    extracted = stream.extractfile(candidate_info)
    if extracted is None:
        raise PublicationCapsuleError("publication capsule candidate identity is unavailable")
    with extracted:
        payload = extracted.read(42)
    if payload != (expected_candidate + "\n").encode("ascii"):
        raise PublicationCapsuleError("publication capsule candidate identity does not match")
    return tuple(admitted)


def unpack_capsule(
    capsule: Path,
    destination: Path,
    *,
    expected_candidate: str,
) -> JsonObject:
    """Validate every member before no-clobber extraction into one safe directory."""

    try:
        capsule_file = validate_regular_file(capsule, maximum_bytes=MAX_CAPSULE_BYTES)
        root = validate_directory(destination)
    except UnsafePathError as exc:
        raise PublicationCapsuleError("publication capsule or destination path is unsafe") from exc
    for name in (ENVIRONMENT_ROOT, *FIXED_FILE_MEMBERS):
        if (root / name).exists():
            raise PublicationCapsuleError("publication capsule extraction target already exists")
    try:
        with tarfile.open(capsule_file, "r:gz") as stream:
            admitted = _validated_members(stream, expected_candidate=expected_candidate)
            for info, name in admitted:
                target = root.joinpath(*PurePosixPath(name).parts)
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=False)
                    target.chmod(info.mode & 0o777)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = stream.extractfile(info)
                if source is None:
                    raise PublicationCapsuleError(
                        "publication capsule regular member is unavailable"
                    )
                observed = 0
                with source, target.open("xb") as output:
                    while True:
                        chunk = source.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > info.size or observed > MAX_MEMBER_BYTES:
                            raise PublicationCapsuleError(
                                "publication capsule member size disagrees with content"
                            )
                        output.write(chunk)
                if observed != info.size:
                    raise PublicationCapsuleError(
                        "publication capsule member size disagrees with content"
                    )
                target.chmod(info.mode & 0o777)
    except PublicationCapsuleError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise PublicationCapsuleError(
            "publication capsule is corrupt or cannot be unpacked"
        ) from exc
    return {
        "candidate_commit": expected_candidate,
        "file_count": len(admitted),
        "schema": "h3-context-registry-publication-capsule/1",
        "status": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--environment", type=Path, required=True)
    create.add_argument("--archive", type=Path, required=True)
    create.add_argument("--report", type=Path, required=True)
    create.add_argument("--candidate", required=True)
    unpack = commands.add_parser("unpack")
    unpack.add_argument("--capsule", type=Path, required=True)
    unpack.add_argument("--destination", type=Path, default=Path("."))
    unpack.add_argument("--expected-candidate", required=True)
    digest = commands.add_parser("digest")
    digest.add_argument("--capsule", type=Path, required=True)
    digest.add_argument("--github-output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--capsule", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_capsule(
                args.output,
                environment=args.environment,
                archive=args.archive,
                report=args.report,
                candidate=args.candidate,
            )
        elif args.command == "unpack":
            result = unpack_capsule(
                args.capsule,
                args.destination,
                expected_candidate=args.expected_candidate,
            )
        elif args.command == "digest":
            capsule_sha256 = compute_capsule_sha256(args.capsule)
            with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"sha256={capsule_sha256}\n")
            result = {
                "capsule_sha256": capsule_sha256,
                "schema": "h3-context-registry-publication-capsule-digest/1",
                "status": "PASS",
            }
        else:
            capsule_sha256 = verify_capsule_sha256(args.capsule, args.expected_sha256)
            result = {
                "capsule_sha256": capsule_sha256,
                "schema": "h3-context-registry-publication-capsule-digest/1",
                "status": "PASS",
            }
    except (OSError, PublicationCapsuleError, UnsafePathError) as exc:
        print(f"REGISTRY PUBLICATION CAPSULE: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
