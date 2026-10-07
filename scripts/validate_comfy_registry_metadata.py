"""Validate the repository's public Comfy Registry metadata without network access.

The validator intentionally reads only local project files. It does not import the runtime package,
resolve dependencies, call GitHub, or inspect credentials. ``--require-finalized`` retains the
publication-workflow interface while validating a normalized three-part semantic version; it never
pins a release number.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import tomli

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.public_projection import allowed as projection_path_allowed  # noqa: E402

EXPECTED_NAME = "minimax-h3-studio"
EXPECTED_AUTHOR = "rookiestar28"
EXPECTED_PUBLISHER = "rookiestar"
EXPECTED_DISPLAY_NAME = "ComfyUI-MiniMaxH3-Studio"
EXPECTED_REPOSITORY = "https://github.com/rookiestar28/ComfyUI-MiniMaxH3-Studio"
EXPECTED_CLASSIFIERS = [
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
]
EXPECTED_ICON_URL = "https://raw.githubusercontent.com/rookiestar28/ComfyUI-MiniMaxH3-Studio/main/assets/h3_context.svg"
EXPECTED_INCLUDES = frozenset(
    {
        "__init__.py",
        "comfyui_h3_context",
        "assets",
        "docs",
        "examples",
        "subgraphs",
        "workflows",
        "README.md",
        "LICENSE",
        "NOTICE",
        ".pre-commit-config.yaml",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
    }
)
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SUPERSEDED_IDENTITIES = (
    "ComfyUI-MinimaxH3-Context",
    "ComfyUI-H3-Context",
    "ComfyUI H3 Context",
    "ComfyUI-Minimax-Context",
    "ComfyUI-Minimax_Context",
)
STABLE_LEGACY_SCHEMA_IDS = {
    "governance/contracts/sidebar_action_v1.schema.json": (
        "https://rookiestar28.github.io/ComfyUI-MinimaxH3-Context/"
        "contracts/sidebar_action_v1.schema.json"
    ),
    "governance/contracts/sidebar_transfer_v1.schema.json": (
        "https://rookiestar28.github.io/ComfyUI-MinimaxH3-Context/"
        "contracts/sidebar_transfer_v1.schema.json"
    ),
    "governance/contracts/sidebar_workspace_v1.schema.json": (
        "https://rookiestar28.github.io/ComfyUI-MinimaxH3-Context/"
        "contracts/sidebar_workspace_v1.schema.json"
    ),
}
PUBLIC_IDENTITY_PATHS = (
    ".github/workflows/publish.yml",
    # IMPORTANT: prose stays outside content validation; TEST_SOP section 2 keeps it rewritable.
    "comfyui_h3_context/__init__.py",
    "governance/contracts/capability_manifest_v1.schema.json",
    "governance/contracts/contracts_v2.schema.json",
    "governance/contracts/h3_context_report_v1.schema.json",
    "governance/contracts/h3_context_v1.schema.json",
    "governance/contracts/h3_prompt_profile_v1.schema.json",
    "governance/contracts/installation_profiles_v1.schema.json",
    "governance/contracts/performance_gate_v1.schema.json",
    "governance/contracts/performance_profiles_v1.schema.json",
    "governance/contracts/performance_qualification_v1.schema.json",
    "governance/contracts/sbom.spdx.json",
    "governance/contracts/sidebar_action_v1.schema.json",
    "governance/contracts/sidebar_transfer_v1.schema.json",
    "governance/contracts/sidebar_workspace_v1.schema.json",
    "governance/contracts/supply_chain_v1.json",
    "governance/contracts/supply_chain_v1.schema.json",
    "comfyui_h3_context/web/h3-context-sidebar.js",
    "frontend/buildMetadata.ts",
    "frontend/src/buildMetadata.ts",
    "pyproject.toml",
    "scripts/m16_01_source_requalification.py",
    "scripts/roadmap_registry.py",
    "scripts/security_audit.py",
    "scripts/supply_chain_manifest.py",
    "tests/fixtures/m16_01_source_requalification.json",
)


class MetadataError(ValueError):
    """Raised when a required public registry contract is invalid."""


def table(value: object, field: str) -> dict[str, Any]:
    """Return a TOML table or raise a field-specific validation error."""

    if not isinstance(value, dict):
        raise MetadataError(f"{field} must be a TOML table")
    return value


def string(table_value: dict[str, Any], field: str) -> str:
    """Return a required string field."""

    value = table_value.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(f"{field} must be a non-empty string")
    return value


def string_list(table_value: dict[str, Any], field: str) -> list[str]:
    """Return a list containing only strings."""

    value = table_value.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MetadataError(f"{field} must be a list of strings")
    return value


def validate_identity_text(relative: str, text: str) -> None:
    """Reject superseded current branding except exact compatibility-frozen schema identifiers."""

    remainder = text
    stable_id = STABLE_LEGACY_SCHEMA_IDS.get(relative)
    if stable_id is not None:
        remainder = remainder.replace(stable_id, "")
    for superseded in SUPERSEDED_IDENTITIES:
        if superseded in remainder:
            raise MetadataError(f"superseded public identity in {relative}: {superseded}")


def validate_public_identity(
    *, source_root: Path | None = None, public_projection: bool = False
) -> None:
    """Validate the closed current/shipping identity inventory without scanning history."""

    root = ROOT if source_root is None else source_root
    for relative in PUBLIC_IDENTITY_PATHS:
        if public_projection and not projection_path_allowed(relative):
            continue
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise MetadataError(f"public identity path must be a regular file: {relative}")
        validate_identity_text(relative, path.read_text(encoding="utf-8"))

    for relative, expected_id in STABLE_LEGACY_SCHEMA_IDS.items():
        if public_projection:
            continue
        schema_text = (root / relative).read_text(encoding="utf-8")
        if f'"$id": "{expected_id}"' not in schema_text:
            raise MetadataError(f"stable schema identity changed: {relative}")


def validate_metadata(
    *, require_finalized: bool, source_root: Path | None = None, public_projection: bool = False
) -> str:
    """Validate metadata, file includes, and the root ComfyUI loader."""

    root = ROOT if source_root is None else source_root.resolve()
    if public_projection and source_root is None:
        raise MetadataError("public projection requires an explicit source root")
    metadata_path = root / "pyproject.toml"
    if not metadata_path.is_file():
        raise MetadataError("pyproject.toml is missing")
    try:
        with metadata_path.open("rb") as stream:
            document = tomli.load(stream)
    except (OSError, tomli.TOMLDecodeError) as exc:
        raise MetadataError(f"cannot parse pyproject.toml: {exc}") from exc

    project = table(document.get("project"), "[project]")
    comfy = table(document.get("tool", {}).get("comfy"), "[tool.comfy]")
    urls = table(project.get("urls"), "[project.urls]")
    license_value = project.get("license")

    if string(project, "name") != EXPECTED_NAME:
        raise MetadataError(f"project.name must be {EXPECTED_NAME!r}")
    version = string(project, "version")
    if not SEMVER_PATTERN.fullmatch(version):
        qualifier = "finalized " if require_finalized else ""
        raise MetadataError(f"{qualifier}project.version must be a three-part semantic version")
    if string(project, "requires-python") != ">=3.10":
        raise MetadataError("project.requires-python must remain >=3.10")
    authors = project.get("authors")
    if authors != [{"name": EXPECTED_AUTHOR}]:
        raise MetadataError("project.authors must identify the repository owner")
    dependencies = project.get("dependencies")
    # CRITICAL: pip installation must not mutate the operator-owned ComfyUI frontend.
    if dependencies != []:
        raise MetadataError("project.dependencies must stay empty for host-owned runtimes")
    if project.get("classifiers") != EXPECTED_CLASSIFIERS:
        raise MetadataError("project.classifiers must match the qualified OS/Python set")
    if license_value != "Apache-2.0":
        raise MetadataError("project.license must use the SPDX expression Apache-2.0")
    if string_list(project, "license-files") != ["LICENSE", "NOTICE"]:
        raise MetadataError("project.license-files must contain LICENSE and NOTICE")
    if string(urls, "Repository") != EXPECTED_REPOSITORY:
        raise MetadataError("project.urls.Repository does not match the repository owner")

    if string(comfy, "PublisherId") != EXPECTED_PUBLISHER:
        raise MetadataError("tool.comfy.PublisherId does not match the Registry publisher")
    if string(comfy, "DisplayName") != EXPECTED_DISPLAY_NAME:
        raise MetadataError("tool.comfy.DisplayName is not the expected public name")
    if string(comfy, "Icon") != EXPECTED_ICON_URL:
        raise MetadataError("tool.comfy.Icon must point to the repository-owned public SVG")
    if "requires-comfyui" in comfy:
        raise MetadataError("tool.comfy.requires-comfyui must not pin a host version")
    includes = frozenset(string_list(comfy, "includes"))
    if includes != EXPECTED_INCLUDES:
        missing = sorted(EXPECTED_INCLUDES - includes)
        unexpected = sorted(includes - EXPECTED_INCLUDES)
        raise MetadataError(
            f"tool.comfy.includes mismatch; missing={missing}, unexpected={unexpected}"
        )

    for relative in sorted(includes):
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            raise MetadataError(f"include escapes repository: {relative}")
        if not candidate.exists():
            raise MetadataError(f"included path is missing: {relative}")

    icon_path = root / "assets" / "h3_context.svg"
    icon_text = icon_path.read_text(encoding="utf-8")
    if "<script" in icon_text.lower() or "xlink:href" in icon_text.lower():
        raise MetadataError("icon must not contain executable or external SVG references")
    loader_text = (root / "__init__.py").read_text(encoding="utf-8")
    for export in ("NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"):
        if export not in loader_text:
            raise MetadataError(f"root loader does not export {export}")
    for required in ("README.md", "LICENSE", "NOTICE", "MANIFEST.in", ".comfyignore"):
        if not (root / required).is_file():
            raise MetadataError(f"public packaging file is missing: {required}")
    # CRITICAL: validate the identities actually shipped by this source policy. A reduced
    # projection must never fabricate absent development inputs to satisfy metadata validation.
    validate_public_identity(source_root=root, public_projection=public_projection)
    return version


def main(argv: list[str] | None = None) -> int:
    """Run validation and return a shell-friendly status code."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--public-projection", action="store_true")
    parser.add_argument(
        "--require-finalized",
        action="store_true",
        help="require a normalized finalized three-part semantic version",
    )
    args = parser.parse_args(argv)
    try:
        version = validate_metadata(
            require_finalized=args.require_finalized,
            source_root=args.source_root,
            public_projection=args.public_projection,
        )
    except (MetadataError, OSError) as exc:
        print(f"Comfy Registry metadata: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"Comfy Registry metadata: PASS ({EXPECTED_NAME} {version}; publisher={EXPECTED_PUBLISHER})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
