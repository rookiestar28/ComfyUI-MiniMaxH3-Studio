"""Run the offline M8-04 clean-artifact release matrix.

This harness is intentionally provider-free and network-free. It installs one wheel and one source
distribution into separate temporary targets with the active interpreter, blocks optional imports,
and runs the same bounded contract probes in fresh child interpreters. It never executes a host,
loads media, resolves a URL, or mutates the repository or a ComfyUI installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, cast

import tomli

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct script execution must import this exact worktree, not a stale installed package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    read_regular_file_bytes,
    validate_directory,
    validate_regular_file,
)
from comfyui_h3_context.core.workflow_migration import (  # noqa: E402
    MigrationDisposition,
    audit_workflow_migration,
    decode_workflow_json,
)

MANIFEST_PATH = ROOT / "tests" / "fixtures" / "m8_04_release_matrix.json"
TIMEOUT_SECONDS = 120
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4_096
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_RELEASE_MANIFEST_WIRE_BYTES = 32_768
MAX_RELEASE_MANIFEST_JSON_DEPTH = 16
MAX_RELEASE_MANIFEST_JSON_ITEMS = 1_024
MAX_RELEASE_MANIFEST_TEXT_LENGTH = 4_096
MAX_PROJECT_METADATA_BYTES = 64 * 1024
TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".py",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
TEXT_BASENAMES = frozenset({"license", "metadata", "notice", "pkg-info", "record", "wheel"})


class ReleaseMatrixError(RuntimeError):
    """Raised when a release matrix lane fails closed."""


JsonObject = dict[str, Any]


def _project_version() -> str:
    """Read the stable release version from the package metadata authority."""

    try:
        payload = read_regular_file_bytes(
            ROOT / "pyproject.toml", maximum_bytes=MAX_PROJECT_METADATA_BYTES
        )
        document = tomli.loads(payload.decode("utf-8-sig", errors="strict"))
        version = document["project"]["version"]
    except (
        OSError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        UnsafePathError,
        tomli.TOMLDecodeError,
    ) as exc:
        raise ReleaseMatrixError("project release version cannot be read") from exc
    if (
        type(version) is not str
        or re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version) is None
    ):
        raise ReleaseMatrixError("project release version is not stable three-part SemVer")
    return version


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ReleaseMatrixError(f"{field} must be an object")
    return cast(JsonObject, value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseMatrixError(f"{field} must be a non-empty string")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReleaseMatrixError(f"{field} must be a list of strings")
    if len(value) != len(set(value)):
        raise ReleaseMatrixError(f"{field} contains duplicate values")
    return tuple(value)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseMatrixError(f"release matrix JSON contains duplicate member {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ReleaseMatrixError(f"release matrix JSON contains invalid constant {value!r}")


def _resources(value: object, *, depth: int = 0, count: list[int]) -> None:
    if depth > MAX_RELEASE_MANIFEST_JSON_DEPTH:
        raise ReleaseMatrixError("release matrix JSON exceeds the depth limit")
    count[0] += 1
    if count[0] > MAX_RELEASE_MANIFEST_JSON_ITEMS:
        raise ReleaseMatrixError("release matrix JSON exceeds the item limit")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str or len(key) > 256:
                raise ReleaseMatrixError("release matrix JSON member name is unbounded")
            _resources(child, depth=depth + 1, count=count)
    elif type(value) is list:
        for child in cast(list[object], value):
            _resources(child, depth=depth + 1, count=count)
    elif value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > MAX_RELEASE_MANIFEST_TEXT_LENGTH:
            raise ReleaseMatrixError("release matrix JSON text is unbounded")
    else:
        raise ReleaseMatrixError("release matrix JSON contains an unsupported type")


def decode_release_manifest_json(payload: str | bytes | bytearray) -> object:
    """Decode exact built-in strict JSON within the release-manifest resource bounds."""

    # CRITICAL: exact built-in admission prevents subclasses from spoofing byte bounds.
    if type(payload) is str:
        try:
            encoded = str.encode(payload, "utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ReleaseMatrixError("release matrix JSON is not strict UTF-8") from exc
    elif type(payload) is bytes:
        encoded = payload
    elif type(payload) is bytearray:
        encoded = bytes(payload)
    else:
        raise ReleaseMatrixError("release matrix JSON requires exact text or bytes")
    if not encoded or len(encoded) > MAX_RELEASE_MANIFEST_WIRE_BYTES:
        raise ReleaseMatrixError("release matrix JSON exceeds the bounded byte limit")
    try:
        value = json.loads(
            encoded.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except ReleaseMatrixError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReleaseMatrixError("release matrix JSON is not strict JSON") from exc
    _resources(value, count=[0])
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> JsonObject:
    """Load and validate the bounded machine-readable release matrix."""

    try:
        payload = read_regular_file_bytes(path, maximum_bytes=MAX_RELEASE_MANIFEST_WIRE_BYTES)
        value = decode_release_manifest_json(payload)
    except UnsafePathError as exc:
        raise ReleaseMatrixError(
            "release matrix path contains a link or reparse component"
        ) from exc
    except OSError as exc:
        raise ReleaseMatrixError("release matrix manifest cannot be read") from exc
    manifest = _object(value, "manifest")
    if set(manifest) != {"schema", "package", "artifact", "offline", "contracts", "host"}:
        raise ReleaseMatrixError("release matrix manifest contains undeclared members")
    if manifest.get("schema") != "h3-context-release-matrix/1":
        raise ReleaseMatrixError("unsupported release matrix schema")
    project_version = _project_version()
    package = _object(manifest.get("package"), "manifest.package")
    if package != {
        "name": "minimax-h3-studio",
        "version": project_version,
        "author": "rookiestar28",
        # CRITICAL: a wheel install must never upgrade or downgrade the host frontend.
        "dependencies": [],
    }:
        raise ReleaseMatrixError("release matrix package identity is not finalized")
    artifact = _object(manifest.get("artifact"), "manifest.artifact")
    if set(artifact) != {
        "required",
        "prefix",
        "forbidden_path_markers",
        "forbidden_suffixes",
        "forbidden_content_markers",
    }:
        raise ReleaseMatrixError("release matrix artifact contains undeclared members")
    if _string_list(artifact.get("required"), "manifest.artifact.required") != (
        "wheel",
        "sdist",
    ):
        raise ReleaseMatrixError("release matrix must require wheel and sdist")
    # CRITICAL: build filenames use the distribution name, not the stable Python import name.
    # Reusing the import identity here rejects the actual Studio wheel and sdist.
    if _string(artifact.get("prefix"), "manifest.artifact.prefix") != (
        f"minimax_h3_studio-{project_version}"
    ):
        raise ReleaseMatrixError("release artifact prefix is not finalized")
    _string_list(artifact.get("forbidden_path_markers"), "manifest.artifact.forbidden_path_markers")
    _string_list(artifact.get("forbidden_suffixes"), "manifest.artifact.forbidden_suffixes")
    if not _string_list(
        artifact.get("forbidden_content_markers"),
        "manifest.artifact.forbidden_content_markers",
    ):
        raise ReleaseMatrixError("artifact content markers must fail closed")
    offline = _object(manifest.get("offline"), "manifest.offline")
    if set(offline) != {"optional_import_roots", "probe_repetitions", "pip_flags"}:
        raise ReleaseMatrixError("release matrix offline section contains undeclared members")
    optional = _string_list(
        offline.get("optional_import_roots"), "manifest.offline.optional_import_roots"
    )
    if not optional or any("." in root or "/" in root for root in optional):
        raise ReleaseMatrixError("optional import roots must be bounded top-level names")
    if offline.get("probe_repetitions") != 2:
        raise ReleaseMatrixError("determinism probe must run exactly twice")
    if _string_list(offline.get("pip_flags"), "manifest.offline.pip_flags") != (
        "--no-index",
        "--no-deps",
        "--no-build-isolation",
    ):
        raise ReleaseMatrixError("offline pip flags are not fail-closed")
    contracts = _object(manifest.get("contracts"), "manifest.contracts")
    if set(contracts) != {
        "base_migration_schema",
        "reference_migration_schema",
        "cancellation_stage",
        "coinstall_foreign_id",
    }:
        raise ReleaseMatrixError("release matrix contracts contain undeclared members")
    for field in (
        "base_migration_schema",
        "reference_migration_schema",
        "cancellation_stage",
        "coinstall_foreign_id",
    ):
        _string(contracts.get(field), f"manifest.contracts.{field}")
    host = _object(manifest.get("host"), "manifest.host")
    if set(host) != {"required", "version", "revision_parts", "fixture_ids"}:
        raise ReleaseMatrixError("release matrix host section contains undeclared members")
    if host.get("required") is not True:
        raise ReleaseMatrixError("M8-04 host lane must remain required")
    if _string(host.get("version"), "manifest.host.version") != "0.32.0":
        raise ReleaseMatrixError("host version is not pinned")
    revision = "".join(_string_list(host.get("revision_parts"), "manifest.host.revision_parts"))
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseMatrixError("host revision must be a 40-character SHA")
    fixture_ids = _string_list(host.get("fixture_ids"), "manifest.host.fixture_ids")
    if fixture_ids != ("m3-07-h3-context-base", "m3-07-h3-context-reference"):
        raise ReleaseMatrixError("host lane must require Base and Reference fixtures")
    return manifest


def _canonical_archive_path(name: str) -> str:
    slash_normalized = name.replace("\\", "/")
    normalized = unicodedata.normalize("NFC", slash_normalized)
    parts = normalized.split("/")
    path_parts = parts[:-1] if parts and parts[-1] == "" else parts
    if (
        not normalized
        or slash_normalized != name
        or normalized != slash_normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        raise ReleaseMatrixError("archive unsafe path")
    return normalized


def _is_public_text(name: str) -> bool:
    path = Path(name)
    return path.suffix.casefold() in TEXT_SUFFIXES or path.name.casefold() in TEXT_BASENAMES


def _validate_member_set(
    artifact: Path,
    members: tuple[tuple[str, int, bool, bytes | None], ...],
    *,
    path_markers: tuple[str, ...],
    suffixes: tuple[str, ...],
    content_markers: tuple[str, ...],
) -> tuple[str, ...]:
    names = _validate_member_metadata(
        artifact,
        members,
        path_markers=path_markers,
        suffixes=suffixes,
    )
    _validate_member_payloads(artifact, members, names, content_markers=content_markers)
    return names


def _validate_member_metadata(
    artifact: Path,
    members: tuple[tuple[str, int, bool, bytes | None], ...],
    *,
    path_markers: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> tuple[str, ...]:
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise ReleaseMatrixError(f"archive member count is outside the bound: {artifact.name}")
    names: list[str] = []
    seen: set[str] = set()
    expanded = 0
    for raw_name, size, _is_directory, _payload in members:
        name = _canonical_archive_path(raw_name)
        collision_key = name.casefold()
        if collision_key in seen:
            raise ReleaseMatrixError(f"archive collision: {artifact.name}:{name}")
        seen.add(collision_key)
        names.append(name)
        if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ReleaseMatrixError(f"archive member size is outside the bound: {artifact.name}")
        expanded += size
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ReleaseMatrixError(f"archive expanded size is outside the bound: {artifact.name}")
        if any(marker.casefold() in name.casefold() for marker in path_markers):
            raise ReleaseMatrixError(f"forbidden archive path: {artifact.name}:{name}")
        if any(name.casefold().endswith(suffix.casefold()) for suffix in suffixes):
            raise ReleaseMatrixError(f"private/heavy archive payload: {artifact.name}:{name}")
    return tuple(names)


def _validate_member_payloads(
    artifact: Path,
    members: tuple[tuple[str, int, bool, bytes | None], ...],
    names: tuple[str, ...],
    *,
    content_markers: tuple[str, ...],
) -> None:
    folded_content_markers = tuple(marker.casefold() for marker in content_markers)
    for (_raw_name, size, is_directory, payload), name in zip(members, names, strict=True):
        if is_directory or not _is_public_text(name):
            continue
        if payload is None or len(payload) != size:
            raise ReleaseMatrixError(f"archive member could not be read: {artifact.name}:{name}")
        try:
            text = payload.decode("utf-8", errors="strict").casefold()
        except UnicodeDecodeError as exc:
            raise ReleaseMatrixError(
                f"public archive text is not strict UTF-8: {artifact.name}:{name}"
            ) from exc
        if any(marker in text for marker in folded_content_markers):
            raise ReleaseMatrixError(f"forbidden archive content: {artifact.name}:{name}")


def _bounded_member_read(stream: Any, *, declared_size: int, artifact: Path, name: str) -> bytes:
    chunks: list[bytes] = []
    remaining = declared_size + 1
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        if type(chunk) is not bytes:
            raise ReleaseMatrixError(f"archive member could not be read: {artifact.name}:{name}")
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) != declared_size:
        raise ReleaseMatrixError(f"archive member size mismatch: {artifact.name}:{name}")
    return payload


def _inspect_archive(
    artifact: Path,
    *,
    path_markers: tuple[str, ...],
    suffixes: tuple[str, ...],
    content_markers: tuple[str, ...],
) -> tuple[str, ...]:
    try:
        artifact = validate_regular_file(artifact, maximum_bytes=MAX_ARTIFACT_BYTES)
    except UnsafePathError as exc:
        raise ReleaseMatrixError("artifact path contains a link or reparse component") from exc
    if artifact.stat().st_size <= 0:
        raise ReleaseMatrixError(f"artifact size is outside the finite bound: {artifact.name}")
    try:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                zip_members = archive.infolist()
                members: list[tuple[str, int, bool, bytes | None]] = []
                for info in zip_members:
                    mode = info.external_attr >> 16
                    member_type = stat.S_IFMT(mode)
                    if member_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ReleaseMatrixError(
                            f"archive unsafe member type: {artifact.name}:{info.filename}"
                        )
                    is_directory = info.is_dir()
                    members.append((info.filename, info.file_size, is_directory, None))
                frozen = tuple(members)
                names = _validate_member_metadata(
                    artifact,
                    frozen,
                    path_markers=path_markers,
                    suffixes=suffixes,
                )
                hydrated: list[tuple[str, int, bool, bytes | None]] = []
                for info, descriptor, name in zip(zip_members, frozen, names, strict=True):
                    raw_name, size, is_directory, _payload = descriptor
                    payload = None
                    if not is_directory and _is_public_text(name):
                        with archive.open(info, "r") as stream:
                            payload = _bounded_member_read(
                                stream,
                                declared_size=size,
                                artifact=artifact,
                                name=name,
                            )
                    hydrated.append((raw_name, size, is_directory, payload))
                _validate_member_payloads(
                    artifact,
                    tuple(hydrated),
                    names,
                    content_markers=content_markers,
                )
                return names
        if artifact.name.endswith(".tar.gz"):
            with tarfile.open(artifact, "r:gz") as archive:
                tar_members = archive.getmembers()
                if any(not (member.isdir() or member.isreg()) for member in tar_members):
                    raise ReleaseMatrixError(f"archive unsafe member type: {artifact.name}")
                members = []
                for tar_info in tar_members:
                    members.append((tar_info.name, tar_info.size, tar_info.isdir(), None))
                frozen = tuple(members)
                names = _validate_member_metadata(
                    artifact,
                    frozen,
                    path_markers=path_markers,
                    suffixes=suffixes,
                )
                hydrated = []
                for tar_info, descriptor, name in zip(tar_members, frozen, names, strict=True):
                    raw_name, size, is_directory, _payload = descriptor
                    payload = None
                    if not is_directory and _is_public_text(name):
                        extracted = archive.extractfile(tar_info)
                        if extracted is None:
                            raise ReleaseMatrixError(
                                f"archive member could not be read: {artifact.name}:{name}"
                            )
                        try:
                            payload = _bounded_member_read(
                                extracted,
                                declared_size=size,
                                artifact=artifact,
                                name=name,
                            )
                        finally:
                            extracted.close()
                    hydrated.append((raw_name, size, is_directory, payload))
                _validate_member_payloads(
                    artifact,
                    tuple(hydrated),
                    names,
                    content_markers=content_markers,
                )
                return names
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ReleaseMatrixError(f"cannot inspect artifact: {artifact.name}") from exc
    raise ReleaseMatrixError(f"unsupported artifact type: {artifact.name}")


def artifact_inventory(artifact_dir: Path, manifest: JsonObject) -> tuple[Path, Path]:
    """Validate the exact two-artifact release boundary and return wheel, sdist."""

    try:
        artifact_dir = validate_directory(artifact_dir)
    except UnsafePathError as exc:
        raise ReleaseMatrixError("artifact root contains a link or reparse component") from exc
    artifacts: list[Path] = []
    for path in sorted(artifact_dir.iterdir()):
        try:
            artifacts.append(validate_regular_file(path, maximum_bytes=MAX_ARTIFACT_BYTES))
        except UnsafePathError as exc:
            raise ReleaseMatrixError("artifact path contains a link or reparse component") from exc
    if len(artifacts) != 2:
        raise ReleaseMatrixError(f"expected exactly two artifacts, got {len(artifacts)}")
    prefix = _string(
        _object(manifest["artifact"], "manifest.artifact")["prefix"], "artifact prefix"
    )
    wheel: Path | None = None
    sdist: Path | None = None
    artifact_spec = _object(manifest["artifact"], "manifest.artifact")
    markers = _string_list(
        artifact_spec["forbidden_path_markers"], "artifact forbidden path markers"
    )
    suffixes = _string_list(artifact_spec["forbidden_suffixes"], "artifact forbidden suffixes")
    content_markers = _string_list(
        artifact_spec["forbidden_content_markers"], "artifact forbidden content markers"
    )
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            if wheel is not None or not artifact.name.startswith(prefix + "-"):
                raise ReleaseMatrixError("wheel artifact is missing or duplicated")
            wheel = artifact
        elif artifact.name.endswith(".tar.gz"):
            if sdist is not None or artifact.name not in {prefix + ".tar.gz"}:
                raise ReleaseMatrixError("sdist artifact is missing or duplicated")
            sdist = artifact
        else:
            raise ReleaseMatrixError(f"unexpected artifact name: {artifact.name}")
        names = _inspect_archive(
            artifact,
            path_markers=markers,
            suffixes=suffixes,
            content_markers=content_markers,
        )
        if not any(
            name.startswith("comfyui_h3_context") or "/comfyui_h3_context/" in name
            for name in names
        ):
            raise ReleaseMatrixError(f"package payload is missing: {artifact.name}")
    if wheel is None or sdist is None:
        raise ReleaseMatrixError("both wheel and sdist are required")
    return wheel, sdist


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_workflow_migrations() -> dict[str, object]:
    """Bind release evidence to the same versioned migration audit used by the host lane."""

    paths = {
        "product_shell_base": ROOT / "workflows" / "m15_03_product_shell_base.json",
        "product_shell_reference": ROOT / "workflows" / "m15_03_product_shell_reference.json",
        "assistant_base": ROOT / "workflows" / "m15_09_assistant_base.json",
        "assistant_reference": ROOT / "workflows" / "m15_09_assistant_reference.json",
    }
    evidence: dict[str, object] = {}
    for name, path in paths.items():
        try:
            workflow = decode_workflow_json(path.read_bytes())
            audit = audit_workflow_migration(workflow)
        except (OSError, ValueError) as exc:
            raise ReleaseMatrixError(f"workflow migration audit failed for {name}") from exc
        if audit.disposition is not MigrationDisposition.ACCEPTED:
            raise ReleaseMatrixError(
                f"workflow migration audit rejected {name}: {audit.reason_code}"
            )
        evidence[name] = audit.to_wire()
    return evidence


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    failure: str,
    timeout: int = TIMEOUT_SECONDS,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
            # IMPORTANT: never let the workspace source tree or its egg-info satisfy a clean
            # artifact install; the release probe must validate the built artifact itself.
            "PYTHONPATH": "",
        }
    )
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseMatrixError(failure) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-2_000:]
        raise ReleaseMatrixError(f"{failure}: {detail}")


def _venv_python(environment_root: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return environment_root / relative


def _create_venv(builder_python: Path, environment_root: Path) -> Path:
    _run_command(
        [str(builder_python), "-m", "venv", str(environment_root)],
        cwd=ROOT,
        failure="isolated virtual environment creation failed",
    )
    python = _venv_python(environment_root)
    if not python.is_file():
        raise ReleaseMatrixError("isolated virtual environment interpreter is missing")
    return python


def _site_packages(python: Path, environment_root: Path) -> Path:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import json,site; print(json.dumps(site.getsitepackages()))",
        ],
        cwd=environment_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )
    if result.returncode != 0:
        raise ReleaseMatrixError("isolated site-packages discovery failed")
    try:
        candidates = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ReleaseMatrixError("isolated site-packages discovery was malformed") from exc
    roots = [Path(value) for value in candidates if isinstance(value, str)]
    # IMPORTANT: Windows venvs may report the venv root as a site root; select only the
    # contained directory whose leaf is the package installation boundary.
    contained = [
        root
        for root in roots
        if root.is_relative_to(environment_root) and root.name.casefold() == "site-packages"
    ]
    if len(contained) != 1 or not contained[0].is_dir():
        raise ReleaseMatrixError("isolated site-packages root is not uniquely contained")
    return contained[0]


def _build_sdist_wheel(builder_python: Path, sdist: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    _run_command(
        [
            str(builder_python),
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
            str(sdist),
        ],
        cwd=ROOT,
        failure="offline sdist wheel build failed",
    )
    wheels = tuple(output.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseMatrixError("offline sdist wheel build did not produce exactly one wheel")
    return wheels[0]


def _pip_install(
    python: Path,
    artifact: Path,
    flags: tuple[str, ...],
    *,
    force_reinstall: bool = False,
) -> None:
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        *flags,
    ]
    if force_reinstall:
        command.append("--force-reinstall")
    command.append(str(artifact))
    _run_command(
        command,
        cwd=ROOT,
        failure=f"clean install failed for {artifact.name}",
    )


def _pip_uninstall(python: Path) -> None:
    _run_command(
        [
            str(python),
            "-m",
            "pip",
            "uninstall",
            "--yes",
            "--disable-pip-version-check",
            "minimax-h3-studio",
        ],
        cwd=ROOT,
        failure="clean uninstall failed",
    )


def _assert_uninstalled(python: Path, site_packages: Path, sentinel: Path) -> None:
    if not sentinel.is_file() or sentinel.read_text(encoding="utf-8") != "FOREIGN_SENTINEL\n":
        raise ReleaseMatrixError("foreign sentinel did not survive package removal")
    owned = [site_packages / "comfyui_h3_context"]
    # CRITICAL: distribution metadata differs from the stable import name; checking only the
    # latter would accept a partial Studio uninstall with owned metadata still present.
    owned.extend(site_packages.glob("minimax_h3_studio-*.dist-info"))
    owned.extend(site_packages.glob("minimax_h3_studio*.egg-info"))
    owned.extend(site_packages.glob("comfyui_h3_context-*.dist-info"))
    owned.extend(site_packages.glob("comfyui_h3_context*.egg-info"))
    if any(path.exists() for path in owned):
        raise ReleaseMatrixError("package-owned paths remained after uninstall")
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.util; "
            "raise SystemExit(importlib.util.find_spec('comfyui_h3_context') is not None)",
        ],
        cwd=site_packages.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    if result.returncode != 0:
        raise ReleaseMatrixError("package remained importable after uninstall")


def _probe_source(optional_roots: tuple[str, ...]) -> str:
    optional_json = json.dumps(optional_roots)
    return """\
import builtins
import importlib.util
import json
import os
import sys

target = os.environ["H3_RELEASE_TARGET"]
sys.path[:] = [target] + [
    item for item in sys.path
    if "site-packages" not in item.casefold() and "dist-packages" not in item.casefold()
]
blocked = frozenset(json.loads(__OPTIONAL_JSON__))
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".", 1)[0] in blocked:
        raise AssertionError("optional import attempted: " + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import

for root in blocked:
    if importlib.util.find_spec(root) is not None:
        raise AssertionError("optional dependency is present in the clean target: " + root)

import comfyui_h3_context as package
from comfyui_h3_context.core import (
    CancellationScope,
    RawContextRequest,
    TaskMode,
    canonical_fingerprint,
    migrate_base_assistant_widgets,
    migrate_reference_assistant_widgets,
    normalize_request,
)

normalized = normalize_request(
    RawContextRequest(TaskMode.T2VA, "A quiet red kite crosses the sky.", duration_seconds=5.167)
)
if normalized.request is None:
    raise AssertionError("deterministic request normalization failed")
request_projection = {
    "task_mode": normalized.request.task_mode.value,
    "user_intent": normalized.request.user_intent,
    "requested_duration_seconds": repr(normalized.request.requested_duration_seconds),
    "effective_frame_count": normalized.request.effective_frame_count,
    "effective_duration_seconds": str(normalized.request.effective_duration_seconds),
}
fingerprint = canonical_fingerprint(request_projection)
if fingerprint != canonical_fingerprint(request_projection):
    raise AssertionError("canonical request fingerprint changed within one run")

base = migrate_base_assistant_widgets({
        "schema": "h3-context-subgraph-fixture/1",
    "version": 1,
    "widgets_values": ["t2va", "A red kite crosses the sky.", 124],
})
reference = migrate_reference_assistant_widgets({
        "schema": "h3-context-subgraph-fixture/1",
    "version": 1,
    "widgets_values": ["ref2va", "Preserve two supplied pictures.", 124],
})
if base.to_wire()["task_mode"] != "t2va" or reference.to_wire()["task_mode"] != "ref2va":
    raise AssertionError("workflow migration changed task mode")
try:
    migrate_base_assistant_widgets({
        "schema": "h3-context-subgraph-fixture/1",
        "version": 1,
        "widgets_values": ["t2va", "stale", 124],
        "unexpected": True,
    })
except Exception:
    pass
else:
    raise AssertionError("unknown migration fields were accepted")

foreign_id = "Foreign.ReleaseMatrixNode"
foreign = object()
nodes = {foreign_id: foreign}
displays = {foreign_id: "Foreign"}
package.register_nodes(nodes, displays)
if nodes[foreign_id] is not foreign:
    raise AssertionError("co-installed node was overwritten")
project_id = sorted(package.NODE_CLASS_MAPPINGS)[0]
nodes[project_id] = foreign
before = dict(nodes)
before_displays = dict(displays)
try:
    package.register_nodes(nodes, displays)
except Exception:
    pass
else:
    raise AssertionError("registration collision was accepted")
if nodes != before or displays != before_displays:
    raise AssertionError("registration collision mutated host mappings")

scope = CancellationScope()
probe = scope.probe("planning")
scope.cancel("release matrix cancellation")
try:
    probe.checkpoint()
except Exception as exc:
    if "cancelled" not in str(exc).casefold():
        raise AssertionError("cancellation diagnostic is not actionable") from exc
else:
    raise AssertionError("cancelled operation emitted a successful result")

print(json.dumps({
        "package_version": package.__version__,
    "request_fingerprint": fingerprint,
    "migration": "pass",
    "coinstallation": "pass",
    "cancellation": "pass",
    "optional_provider_absence": True,
}, sort_keys=True))
""".replace("__OPTIONAL_JSON__", repr(optional_json))


def _run_probe(
    python: Path,
    target: Path,
    optional_roots: tuple[str, ...],
    workspace: Path,
) -> JsonObject:
    blocker = workspace / "import-guard"
    blocker.mkdir(parents=True, exist_ok=False)
    (blocker / "sitecustomize.py").write_text(
        "# Release matrix import guard is intentionally inert; the probe installs its own guard.\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "H3_RELEASE_TARGET": str(target),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join((str(blocker), str(target))),
        }
    )
    try:
        result = subprocess.run(
            [str(python), "-c", _probe_source(optional_roots)],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env=environment,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout)[-2_000:]
            raise ReleaseMatrixError(f"clean probe failed for {target.name}: {detail}")
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise ReleaseMatrixError(
                f"clean probe returned invalid JSON for {target.name}"
            ) from exc
        return _object(value, "clean probe result")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseMatrixError(f"clean probe failed for {target.name}") from exc
    finally:
        shutil.rmtree(blocker, ignore_errors=True)


def run_matrix(artifact_dir: Path, *, python: Path = Path(sys.executable)) -> JsonObject:
    """Run both clean artifact lanes and return redacted machine-readable evidence."""

    manifest = load_manifest()
    migration_audits = _audit_workflow_migrations()
    if not python.is_file():
        raise ReleaseMatrixError("selected interpreter is unavailable")
    wheel, sdist = artifact_inventory(artifact_dir, manifest)
    package_version = _string(
        _object(manifest["package"], "manifest.package")["version"],
        "manifest.package.version",
    )
    offline = _object(manifest["offline"], "manifest.offline")
    flags = _string_list(offline["pip_flags"], "offline pip flags")
    optional = _string_list(offline["optional_import_roots"], "optional import roots")
    temp_root = ROOT / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    lanes: list[JsonObject] = []
    with tempfile.TemporaryDirectory(prefix="m8-04-release-", dir=temp_root) as temporary:
        root = Path(temporary)
        for kind, artifact in (("wheel", wheel), ("sdist", sdist)):
            lane_root = root / f"lane-{kind}"
            lane_root.mkdir()
            environment_root = lane_root / "venv"
            lane_python = _create_venv(python, environment_root)
            target = _site_packages(lane_python, environment_root)
            sentinel = target / "foreign_release_sentinel.py"
            sentinel.write_text("FOREIGN_SENTINEL\n", encoding="utf-8")
            install_artifact = (
                artifact
                if kind == "wheel"
                else _build_sdist_wheel(python, artifact, lane_root / "sdist-wheel")
            )
            _pip_install(lane_python, install_artifact, flags)
            first = _run_probe(lane_python, target, optional, lane_root)
            second = _run_probe(lane_python, target, optional, lane_root)
            if first != second:
                raise ReleaseMatrixError(f"deterministic offline probe drifted for {kind}")
            if first.get("package_version") != package_version:
                raise ReleaseMatrixError(f"clean {kind} package version is not {package_version}")
            _pip_install(lane_python, install_artifact, flags, force_reinstall=True)
            if _run_probe(lane_python, target, optional, lane_root) != first:
                raise ReleaseMatrixError(f"reinstall probe drifted for {kind}")
            _pip_uninstall(lane_python)
            _assert_uninstalled(lane_python, target, sentinel)
            _pip_install(lane_python, install_artifact, flags)
            if _run_probe(lane_python, target, optional, lane_root) != first:
                raise ReleaseMatrixError(f"rollback probe drifted for {kind}")
            _pip_uninstall(lane_python)
            _assert_uninstalled(lane_python, target, sentinel)
            lanes.append(
                {
                    "artifact": kind,
                    "filename": artifact.name,
                    "sha256": _sha256(artifact),
                    "install": "pass",
                    "reinstall": "pass",
                    "uninstall": "pass",
                    "rollback": "pass",
                    "final_uninstall": "pass",
                    "foreign_sentinel_preserved": True,
                    "target_cleanup_verified": True,
                    "environment_cleanup_verified": False,
                    "probe": first,
                }
            )
    if root.exists():
        raise ReleaseMatrixError("isolated release matrix root was not cleaned")
    for lane in lanes:
        lane["environment_cleanup_verified"] = True
    return {
        "status": "PASS",
        "schema": "h3-context-release-matrix/1",
        "interpreter": str(python),
        "artifact_lanes": lanes,
        "host_lane": "REQUIRED_SEPARATE_H0_H1",
        "network": "disabled",
        "optional_provider_absence": True,
        "migration_audits": migration_audits,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = run_matrix(args.artifact_dir, python=args.python)
    except (OSError, ReleaseMatrixError) as exc:
        print(f"RELEASE MATRIX: FAIL: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(evidence, sort_keys=True))
    else:
        print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
