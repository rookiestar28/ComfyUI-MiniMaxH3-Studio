"""Build and validate the closed license, SPDX, and source/runtime integrity inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import tomli

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct script execution must import this exact worktree, not a stale installed package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.adapters.authoring_fonts import (  # noqa: E402
    FONT_MANIFEST_FILENAME,
    AuthoringFontError,
    load_packaged_font_manifest,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint  # noqa: E402
from comfyui_h3_context.core.product_shell import (  # noqa: E402
    SUPPORTED_FRONTEND_VERSION,
)
from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    validate_directory,
    validate_regular_file,
)

FRONTEND_ROOT = ROOT / "frontend"
LICENSE_INVENTORY_PATH = FRONTEND_ROOT / "license-inventory.json"
SPDX_PATH = ROOT / "governance" / "contracts" / "sbom.spdx.json"
SUPPLY_CHAIN_PATH = ROOT / "governance" / "contracts" / "supply_chain_v1.json"
FONT_MANIFEST_PATH = ROOT / "comfyui_h3_context" / "fonts" / FONT_MANIFEST_FILENAME
FONT_SOURCE_REPOSITORY = "notofonts/latin-greek-cyrillic"
MAX_SUPPLY_CHAIN_WIRE_BYTES = 262_144
MAX_JSON_DEPTH = 24
MAX_JSON_ITEMS = 8_192

JsonObject = dict[str, Any]


class SupplyChainError(ValueError):
    """Raised when supply-chain input or generated state is incomplete or inconsistent."""


def _project_version() -> str:
    """Read the release version without creating a release-number pin in this generator."""

    try:
        with _regular_file(ROOT / "pyproject.toml", "project metadata").open("rb") as stream:
            version = tomli.load(stream)["project"]["version"]
    except (OSError, KeyError, TypeError, tomli.TOMLDecodeError) as exc:
        raise SupplyChainError("project version cannot be read") from exc
    if type(version) is not str or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SupplyChainError("project version must be a three-part semantic version")
    return version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, field: str) -> Path:
    try:
        candidate = validate_regular_file(path)
        repository = validate_directory(ROOT)
    except UnsafePathError as exc:
        raise SupplyChainError(f"{field} contains a link or reparse path") from exc
    if ROOT != candidate and ROOT not in candidate.parents:
        raise SupplyChainError(f"{field} escapes the repository")
    if repository != ROOT:
        raise SupplyChainError("repository root validation failed")
    return candidate


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SupplyChainError(f"supply-chain JSON contains duplicate member {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise SupplyChainError(f"supply-chain JSON contains invalid constant {value!r}")


def _resources(value: object, *, depth: int = 0, count: list[int]) -> None:
    if depth > MAX_JSON_DEPTH:
        raise SupplyChainError("supply-chain JSON exceeds the depth limit")
    count[0] += 1
    if count[0] > MAX_JSON_ITEMS:
        raise SupplyChainError("supply-chain JSON exceeds the item limit")
    if type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            if type(key) is not str or len(key) > 256:
                raise SupplyChainError("supply-chain JSON member name is unbounded")
            _resources(child, depth=depth + 1, count=count)
    elif type(value) is list:
        for child in cast(list[object], value):
            _resources(child, depth=depth + 1, count=count)
    elif value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 4_096:
            raise SupplyChainError("supply-chain JSON text is unbounded")
    else:
        raise SupplyChainError("supply-chain JSON contains an unsupported type")


def _decode_json(payload: str | bytes | bytearray, *, maximum: int) -> object:
    # CRITICAL: exact built-in types prevent hostile subclasses from spoofing resource bounds.
    if type(payload) is str:
        try:
            encoded = str.encode(payload, "utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise SupplyChainError("supply-chain JSON is not strict UTF-8") from exc
    elif type(payload) is bytes:
        encoded = payload
    elif type(payload) is bytearray:
        encoded = bytes(payload)
    else:
        raise SupplyChainError("supply-chain JSON requires exact text or bytes")
    if not encoded or len(encoded) > maximum:
        raise SupplyChainError("supply-chain JSON exceeds the bounded byte limit")
    try:
        value = json.loads(
            encoded.decode("utf-8", "strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except SupplyChainError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SupplyChainError("supply-chain JSON is not strict JSON") from exc
    _resources(value, count=[0])
    return value


def _object(value: object, field: str) -> JsonObject:
    if type(value) is not dict:
        raise SupplyChainError(f"{field} must be an object")
    return cast(JsonObject, value)


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SupplyChainError("supply-chain value is not canonical JSON") from exc


def _pretty_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SupplyChainError("supply-chain value is not pretty JSON") from exc


def frontend_lock_packages(path: Path) -> tuple[tuple[str, str], ...]:
    """Return exact name/version pairs from the pnpm v9 packages section without YAML execution."""

    text = _regular_file(path, "frontend lockfile").read_text(encoding="utf-8", errors="strict")
    in_packages = False
    values: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line == "packages:":
            in_packages = True
            continue
        if line == "snapshots:":
            break
        if not in_packages or not line.startswith("  ") or line.startswith("    "):
            continue
        raw = line[2:]
        if not raw.endswith(":"):
            raise SupplyChainError("pnpm package key is malformed")
        raw = raw[:-1]
        if raw.startswith('"'):
            try:
                key = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SupplyChainError("pnpm package key is malformed") from exc
        else:
            key = raw
        if type(key) is not str or "@" not in key:
            raise SupplyChainError("pnpm package identity is malformed")
        name, version = key.rsplit("@", 1)
        version = version.split("(", 1)[0]
        if not name or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]*", version):
            raise SupplyChainError("pnpm package identity is malformed")
        values.append((name, version))
    result = tuple(sorted(set(values)))
    if len(result) != len(values) or not result:
        raise SupplyChainError("pnpm package inventory is empty or duplicated")
    return result


def collect_frontend_license_inventory() -> list[JsonObject]:
    """Collect installed license metadata and close known platform-optional package families."""

    executable = shutil.which("pnpm")
    if executable is None:
        raise SupplyChainError("pnpm is unavailable")
    try:
        result = subprocess.run(
            [executable, "licenses", "list", "--json"],
            cwd=FRONTEND_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupplyChainError("pnpm license inventory failed") from exc
    if result.returncode != 0:
        raise SupplyChainError("pnpm license inventory failed")
    installed = _object(_decode_json(result.stdout, maximum=2_000_000), "pnpm licenses")
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    for license_name, entries in installed.items():
        if type(license_name) is not str or type(entries) is not list:
            raise SupplyChainError("pnpm license output is malformed")
        for entry_value in entries:
            entry = _object(entry_value, "pnpm license entry")
            name = entry.get("name")
            versions = entry.get("versions")
            if type(name) is not str or type(versions) is not list:
                raise SupplyChainError("pnpm license entry is malformed")
            for version in versions:
                if type(version) is not str:
                    raise SupplyChainError("pnpm license version is malformed")
                mapping[(name, version)] = (license_name, "installed_package_metadata")

    family_licenses = (
        ("@rolldown/binding-", "MIT"),
        ("@typescript/typescript-", "Apache-2.0"),
        ("lightningcss-", "MPL-2.0"),
        ("fsevents", "MIT"),
    )
    inventory: list[JsonObject] = []
    for name, version in frontend_lock_packages(FRONTEND_ROOT / "pnpm-lock.yaml"):
        found = mapping.get((name, version))
        if found is None:
            matches = [
                license_name for prefix, license_name in family_licenses if name.startswith(prefix)
            ]
            if len(matches) != 1:
                raise SupplyChainError(f"license is unclosed for {name}@{version}")
            found = (matches[0], "qualified_platform_family")
        license_name, source = found
        if license_name == "NOASSERTION" or not license_name:
            raise SupplyChainError(f"license is unclosed for {name}@{version}")
        inventory.append(
            {"license": license_name, "name": name, "source": source, "version": version}
        )
    return inventory


def load_frontend_license_inventory() -> list[JsonObject]:
    """Load and close the frozen frontend license inventory against the lockfile."""

    value = _decode_json(
        _regular_file(LICENSE_INVENTORY_PATH, "frontend license inventory").read_bytes(),
        maximum=MAX_SUPPLY_CHAIN_WIRE_BYTES,
    )
    if type(value) is not list:
        raise SupplyChainError("frontend license inventory must be a list")
    inventory = [_object(item, "frontend license inventory entry") for item in value]
    expected = frontend_lock_packages(FRONTEND_ROOT / "pnpm-lock.yaml")
    actual = tuple((item.get("name"), item.get("version")) for item in inventory)
    if actual != expected or len(inventory) != 156:
        raise SupplyChainError("frontend license inventory does not match the frozen lockfile")
    allowed_keys = {"license", "name", "source", "version"}
    if any(set(item) != allowed_keys or item.get("license") == "NOASSERTION" for item in inventory):
        raise SupplyChainError("frontend license inventory is incomplete")
    return inventory


def _spdx_package(
    name: str,
    version: str,
    license_name: str,
    purpose: str,
    comment: str,
    purl: str,
) -> JsonObject:
    token = hashlib.sha256(purl.encode("utf-8")).hexdigest()[:20]
    if purl.startswith("pkg:npm/"):
        download_location = f"https://registry.npmjs.org/{quote(name, safe='@/')}"
    elif purl.startswith("pkg:pypi/"):
        download_location = f"https://pypi.org/project/{quote(name, safe='')}/"
    elif purl.startswith("pkg:github/"):
        repository = purl.removeprefix("pkg:github/").split("@", 1)[0]
        download_location = f"https://github.com/{quote(repository, safe='/')}"
    else:
        raise SupplyChainError(f"unsupported SPDX package ecosystem: {purl}")
    return {
        "SPDXID": f"SPDXRef-Package-{token}",
        "comment": comment,
        "downloadLocation": download_location,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceLocator": purl,
                "referenceType": "purl",
            }
        ],
        "filesAnalyzed": False,
        "licenseConcluded": license_name,
        "licenseDeclared": license_name,
        "name": name,
        "primaryPackagePurpose": purpose,
        "versionInfo": version,
    }


def _bundled_font_spdx_package() -> JsonObject:
    """Build the font package row from the same verified manifest the runtime consumes."""

    try:
        verified = load_packaged_font_manifest()
    except AuthoringFontError as exc:
        raise SupplyChainError("font inventory verification failed") from exc
    manifest = _object(
        _decode_json(
            _regular_file(FONT_MANIFEST_PATH, "packaged font manifest").read_bytes(),
            maximum=MAX_SUPPLY_CHAIN_WIRE_BYTES,
        ),
        "packaged font manifest",
    )
    # CRITICAL: verification and inventory must describe the same manifest bytes. Without this
    # join, a file replacement between the two reads could put unverified revision facts in SPDX.
    if canonical_fingerprint(manifest) != verified.manifest_fingerprint:
        raise SupplyChainError("packaged font manifest changed after verification")

    assets = manifest.get("font_assets")
    if type(assets) is not list or len(assets) != 1:
        raise SupplyChainError("packaged font inventory must contain exactly one asset")
    asset = _object(assets[0], "packaged font asset")
    release_id = asset.get("release_id")
    build_revision = asset.get("build_revision")
    if (
        type(release_id) is not str
        or not release_id
        or len(release_id) > 128
        or type(build_revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", build_revision) is None
    ):
        raise SupplyChainError("packaged font release identity is invalid")
    if not verified.faces or len({face.family_name for face in verified.faces}) != 1:
        raise SupplyChainError("packaged font family identity is invalid")
    family_name = verified.faces[0].family_name
    return _spdx_package(
        family_name,
        release_id,
        verified.license_spdx_id,
        "LIBRARY",
        "bundled_font_runtime",
        f"pkg:github/{FONT_SOURCE_REPOSITORY}@{build_revision}",
    )


def build_spdx_sbom() -> JsonObject:
    """Build the deterministic SPDX 2.3 package inventory."""

    project_version = _project_version()
    packages = [
        _spdx_package(
            "minimax-h3-studio",
            project_version,
            "Apache-2.0",
            "APPLICATION",
            "bundled_project_runtime",
            f"pkg:pypi/minimax-h3-studio@{project_version}",
        ),
        # CRITICAL: single-sourced from the package, never restated. This SBOM entry and the
        # `resolved_dependencies` row in `scripts/build_provenance.py` describe the same
        # dependency; two literals can drift apart while both records still validate, and the
        # supply-chain document would then be quietly wrong about what the product supports.
        _spdx_package(
            "comfyui-frontend-package",
            SUPPORTED_FRONTEND_VERSION,
            "GPL-3.0-only",
            "FRAMEWORK",
            "host_owned_qualification_observation_not_bundled",
            f"pkg:pypi/comfyui-frontend-package@{SUPPORTED_FRONTEND_VERSION}",
        ),
        _spdx_package(
            "ComfyUI",
            "0.32.0+b323a345bbbfb2f3a95b5b73b68eb7919a26515e",
            "GPL-3.0-only",
            "FRAMEWORK",
            "host_owned_not_bundled",
            "pkg:github/Comfy-Org/ComfyUI@b323a345bbbfb2f3a95b5b73b68eb7919a26515e",
        ),
        _spdx_package(
            "Ollama",
            "user-selected-qualified-version",
            "MIT",
            "APPLICATION",
            "external_user_managed_not_bundled",
            "pkg:github/ollama/ollama",
        ),
    ]
    packages.append(_bundled_font_spdx_package())
    python_development = {
        "aiohttp": ("3.11..<4", "Apache-2.0 AND MIT"),
        "build": ("1.5", "MIT"),
        "detect-secrets": ("1.5", "Apache-2.0"),
        "jsonschema": ("4.23", "MIT"),
        "mypy": ("2.3", "MIT"),
        "pre-commit": ("4.6", "MIT"),
        "pytest": ("9.1", "MIT"),
        "pytest-cov": ("7.1", "MIT"),
        "ruff": ("0.16", "MIT"),
        "setuptools": ("80..<84", "MIT"),
        "tomli": ("2.4", "MIT"),
        "types-jsonschema": ("4.23", "Apache-2.0"),
        "wheel": ("0.46.2..<0.47", "MIT"),
    }
    for name, (version, license_name) in sorted(python_development.items()):
        packages.append(
            _spdx_package(
                name,
                version,
                license_name,
                "SOURCE",
                "development_build_only_not_bundled",
                f"pkg:pypi/{name}",
            )
        )
    for item in load_frontend_license_inventory():
        name = cast(str, item["name"])
        version = cast(str, item["version"])
        packages.append(
            _spdx_package(
                name,
                version,
                cast(str, item["license"]),
                "SOURCE",
                "development_build_only_not_bundled",
                f"pkg:npm/{quote(name, safe='/')}@{version}",
            )
        )
    packages.sort(key=lambda item: cast(str, item["SPDXID"]))
    return {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": "2026-08-10T00:00:00Z",
            "creators": ["Tool: scripts/supply_chain_manifest.py"],
        },
        "dataLicense": "CC0-1.0",
        "documentNamespace": (
            f"https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio/sbom/{project_version}"
        ),
        "name": f"ComfyUI-MiniMaxH3-Studio {project_version} dependency inventory",
        "packages": packages,
        "spdxVersion": "SPDX-2.3",
    }


def load_spdx_sbom() -> JsonObject:
    """Load the shipped SPDX document and compare it to current declared inputs."""

    actual = _object(
        _decode_json(
            _regular_file(SPDX_PATH, "SPDX SBOM").read_bytes(),
            maximum=MAX_SUPPLY_CHAIN_WIRE_BYTES,
        ),
        "SPDX SBOM",
    )
    if _canonical_bytes(actual) != _canonical_bytes(build_spdx_sbom()):
        raise SupplyChainError("SPDX SBOM does not match the frozen dependency inventory")
    return actual


def _build_input_paths() -> tuple[Path, ...]:
    fixed = (
        ROOT / ".comfyignore",
        ROOT / ".github" / "workflows" / "publish.yml",
        ROOT / "LICENSE",
        ROOT / "MANIFEST.in",
        ROOT / "NOTICE",
        ROOT / "pyproject.toml",
        # Source selection is build behavior: freeze the backend and its public boundary.
        ROOT / "scripts" / "public_build_backend.py",
        ROOT / "scripts" / "public_source_policy.py",
        FRONTEND_ROOT / "buildMetadata.ts",
        FRONTEND_ROOT / "buildProvenance.ts",
        FRONTEND_ROOT / "license-inventory.json",
        FRONTEND_ROOT / "package.json",
        FRONTEND_ROOT / "pnpm-lock.yaml",
        FRONTEND_ROOT / "tsconfig.json",
        FRONTEND_ROOT / "vite.config.ts",
        # CRITICAL: Vite imports this package-owned authority from outside
        # frontend/src, so the source scan below cannot discover it.
        ROOT / "comfyui_h3_context" / "contracts" / "official_h3_assets_v2.json",
        # HC-09 runtime source imports these package-owned host-seam authorities
        # from outside frontend/src, so the source scan cannot discover them.
        ROOT / "comfyui_h3_context" / "contracts" / "host_seam_census_v1.json",
        ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json",
        ROOT / "comfyui_h3_context" / "contracts" / "installation_profiles_v1.json",
        ROOT / "governance" / "contracts" / "installation_profiles_v1.schema.json",
        ROOT / "comfyui_h3_context" / "contracts" / "build_provenance_v1.json",
        ROOT / "governance" / "contracts" / "build_provenance_v1.schema.json",
        ROOT / "governance" / "contracts" / "supply_chain_v1.schema.json",
        SPDX_PATH,
    )
    sources = tuple(
        path
        for path in (FRONTEND_ROOT / "src").rglob("*")
        if path.is_file() and path.suffix in {".css", ".ts", ".tsx"}
    )
    resolved = tuple(_regular_file(path, "supply-chain build input") for path in fixed + sources)
    if len(set(resolved)) != len(resolved):
        raise SupplyChainError("supply-chain build inputs contain duplicates")
    return tuple(sorted(resolved, key=lambda path: path.relative_to(ROOT).as_posix()))


def build_supply_chain_manifest() -> JsonObject:
    """Build the deterministic file-integrity join over SBOM, source, lock, and runtime bytes."""

    load_spdx_sbom()
    runtime = _regular_file(
        ROOT / "comfyui_h3_context" / "web" / "h3-context-sidebar.js",
        "frontend runtime entry",
    )
    return {
        "build_inputs": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": f"sha256:{_sha256(path)}",
                "size": path.stat().st_size,
            }
            for path in _build_input_paths()
        ],
        "frontend_lock_package_count": len(
            frontend_lock_packages(FRONTEND_ROOT / "pnpm-lock.yaml")
        ),
        "license_inventory": {
            "path": LICENSE_INVENTORY_PATH.relative_to(ROOT).as_posix(),
            "sha256": f"sha256:{_sha256(LICENSE_INVENTORY_PATH)}",
        },
        "reference_assets": "excluded_not_build_inputs_or_artifact_members",
        "runtime_entries": [
            {
                "path": runtime.relative_to(ROOT).as_posix(),
                "sha256": f"sha256:{_sha256(runtime)}",
                "size": runtime.stat().st_size,
            }
        ],
        "schema": "h3-context-supply-chain/1",
        "spdx": {
            "package_count": len(load_spdx_sbom()["packages"]),
            "path": SPDX_PATH.relative_to(ROOT).as_posix(),
            "sha256": f"sha256:{_sha256(SPDX_PATH)}",
            "version": "SPDX-2.3",
        },
        "version": 1,
    }


def decode_supply_chain_json(payload: str | bytes | bytearray) -> JsonObject:
    """Decode and validate one exact supply-chain manifest."""

    actual = _object(_decode_json(payload, maximum=MAX_SUPPLY_CHAIN_WIRE_BYTES), "manifest")
    if _canonical_bytes(actual) != _canonical_bytes(build_supply_chain_manifest()):
        raise SupplyChainError("supply-chain manifest does not match current frozen inputs")
    return actual


def load_supply_chain_manifest() -> JsonObject:
    """Load the shipped supply-chain manifest with strict current-input validation."""

    return decode_supply_chain_json(
        _regular_file(SUPPLY_CHAIN_PATH, "supply-chain manifest").read_bytes()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emit",
        choices=("licenses", "spdx", "manifest", "validate"),
        default="validate",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the deterministic supply-chain manifest before validating it",
    )
    args = parser.parse_args(argv)
    try:
        if args.write:
            if args.emit != "validate":
                raise SupplyChainError("--write is valid only with --emit validate")
            # IMPORTANT: SPDX is an input to the manifest, so regenerate it first.
            SPDX_PATH.write_bytes(_canonical_bytes(build_spdx_sbom()))
            SUPPLY_CHAIN_PATH.write_bytes(_pretty_bytes(build_supply_chain_manifest()))
        if args.emit == "licenses":
            value: object = collect_frontend_license_inventory()
        elif args.emit == "spdx":
            value = build_spdx_sbom()
        elif args.emit == "manifest":
            value = build_supply_chain_manifest()
        else:
            value = load_supply_chain_manifest()
        print(_canonical_bytes(value).decode("utf-8"), end="")
    except (OSError, SupplyChainError) as exc:
        print(f"SUPPLY CHAIN: FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
