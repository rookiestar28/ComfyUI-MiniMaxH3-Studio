"""Independent required-file inventory and isolated public-product smoke checks."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

import tomli

PACKAGE = "comfyui_h3_context"
MAX_FILES = 4096
MAX_SOURCE_BYTES = 16 * 1024 * 1024
DEVELOPMENT_PARTS = frozenset(
    {"tests", "__tests__", "__pycache__", "node_modules", "e2e", "red-tests"}
)
BASE_REQUIRED = frozenset({"__init__.py", "pyproject.toml", f"{PACKAGE}/__init__.py"})
DEVELOPER_ROOTS = frozenset(
    {"tests", "frontend", "scripts", "governance", "compatibility", "requirements"}
)
MAINTAINER_DOCUMENTS = frozenset(
    {
        "tests/TEST_SOP.md",
        "tests/E2E_TESTING_NOTICE.md",
        "tests/E2E_TESTING_SOP.md",
        "tests/CI_TEST_MATRIX.md",
    }
)
WORKFLOW = ".github/workflows/publish.yml"
SMOKE_CHECKS = (
    "root_loader",
    "registration",
    "public_manifest",
    "installation_catalog",
    "prompt_catalogs",
    "semantic_catalog",
    "official_assets",
    "packaged_fonts",
    "json_resources",
)


class SmokeReport(TypedDict):
    status: str
    registered_count: int
    contract_count: int
    checks: list[str]


class CompletenessError(ValueError):
    """A public product is missing a necessary file or cannot load in isolation."""


def _path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in value
        or ":" in value
        or any(not character.isprintable() for character in value)
    ):
        raise CompletenessError("unsafe required path")
    return value


def _development(path: str) -> bool:
    return any(part.casefold() in DEVELOPMENT_PARTS for part in path.split("/"))


def require_present(required: Iterable[str], actual: Iterable[str], *, phase: str) -> None:
    missing = sorted(set(required) - set(actual))
    if missing:
        # IMPORTANT: equality to the packer's own selection cannot prove completeness.
        # The required inventory must originate upstream from source declarations/dependencies.
        raise CompletenessError(f"{phase}: missing required files: {', '.join(missing[:12])}")


def _developer_input(path: str) -> bool:
    parts = tuple(part.casefold() for part in path.split("/"))
    return path not in MAINTAINER_DOCUMENTS and not any(
        part
        in {
            ".planning",
            "reference",
            ".reference",
            ".sessions",
            ".git",
            ".tmp",
            ".cache",
            "__pycache__",
            "node_modules",
            ".venv",
            "agents.md",
            "roadmap.md",
        }
        or part.startswith((".venv-", ".env"))
        or part.endswith((".pyc", ".pyo", ".log"))
        for part in parts
    )


def development_required_paths(available: frozenset[str], metadata: dict[str, Any]) -> set[str]:
    """Require source-declared test/support trees, independently of packer selection."""
    declaration = metadata.get("tool", {}).get("h3-context", {}).get("development")
    if declaration is None:
        return set()
    if not isinstance(declaration, dict):
        raise CompletenessError("invalid developer declarations")
    roots, fixed = declaration.get("roots"), declaration.get("required")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(value, str) and value in DEVELOPER_ROOTS for value in roots)
        or not isinstance(fixed, list)
        or not fixed
        or not all(isinstance(value, str) for value in fixed)
    ):
        raise CompletenessError("invalid developer declarations")
    required = {_path(value) for value in fixed}
    if not all(_developer_input(path) for path in required):
        raise CompletenessError("developer declaration names a private input")
    require_present(required, available, phase="developer source")
    for root in roots:
        matches = {
            path for path in available if path.startswith(root + "/") and _developer_input(path)
        }
        if not matches:
            raise CompletenessError("source: missing required developer root: " + root)
        required.update(matches)
    return required


def required_paths(
    paths: Iterable[str], read: Callable[[str], bytes], *, publication: bool = False
) -> frozenset[str]:
    """Derive requirements without consulting projection or archive filtering rules."""
    available = frozenset(_path(path) for path in paths)
    require_present(BASE_REQUIRED, available, phase="source")

    def bounded_read(path: str) -> bytes:
        payload = read(path)
        if len(payload) > MAX_SOURCE_BYTES:
            raise CompletenessError("required source is outside the size bound")
        return payload

    try:
        metadata = tomli.loads(bounded_read("pyproject.toml").decode("utf-8"))
        includes = metadata["tool"]["comfy"]["includes"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CompletenessError("required runtime declarations are unavailable") from exc
    if (
        not isinstance(includes, list)
        or not includes
        or not all(isinstance(value, str) for value in includes)
    ):
        raise CompletenessError("required runtime declarations are invalid")
    required = set(BASE_REQUIRED)
    # IMPORTANT: tests are a promised public surface. Runtime-only completeness allowed a
    # working node archive to silently discard every test, fixture and execution command.
    required.update(development_required_paths(available, metadata))
    for value in includes:
        declared = _path(value.rstrip("/"))
        matches = {
            path
            for path in available
            if (path == declared or path.startswith(declared + "/")) and not _development(path)
        }
        if not matches:
            raise CompletenessError("source: missing required declared files: " + declared)
        required.update(matches)
    # Include every runtime extension, independently of setuptools or packer globs.
    required.update(
        path for path in available if path.startswith(PACKAGE + "/") and not _development(path)
    )
    if publication:
        require_present({WORKFLOW}, available, phase="publication source")
        required.add(WORKFLOW)
        workflow = "\n".join(
            line
            for line in bounded_read(WORKFLOW).decode("utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        # Literal repository script invocations and requirements are deliberately independent
        # of PUBLICATION_FILES; new helpers need an explicit privacy-reviewed allowlist entry.
        helpers = set(re.findall(r"\b(?:scripts/[\w./-]+|requirements/[\w./-]+)\b", workflow))
        require_present(helpers, available, phase="publication source")
        required.update(helpers)
    pending = [path for path in required if path.endswith(".py")]
    visited: set[str] = set()

    def resolve(module: str, *, local: bool = False, optional: bool = False) -> set[str]:
        stem = module.replace(".", "/")
        matches = {path for path in (stem + ".py", stem + "/__init__.py") if path in available}
        if not matches and not optional and (local or module.startswith(PACKAGE + ".")):
            raise CompletenessError("source: missing required local module: " + stem)
        for ancestor in PurePosixPath(stem).parents:
            initializer = ancestor.as_posix() + "/__init__.py"
            if initializer in available:
                matches.add(initializer)
        return matches

    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            tree = ast.parse(bounded_read(path), filename=path)
        except SyntaxError as exc:
            raise CompletenessError("required Python source is invalid: " + path) from exc
        package = path.removesuffix(".py").split("/")[:-1]
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dependencies.update(resolve(alias.name))
            elif isinstance(node, ast.ImportFrom):
                prefix = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
                if module:
                    dependencies.update(resolve(module, local=bool(node.level)))
                    for alias in node.names:
                        if alias.name != "*":
                            dependencies.update(resolve(module + "." + alias.name, optional=True))
        added = dependencies - required
        required.update(added)
        pending.extend(path for path in added if path.endswith(".py"))
    if len(required) > MAX_FILES:
        raise CompletenessError("required file inventory is outside the bound")
    return frozenset(required)


def checkout_required_paths(root: Path, tracked: Iterable[str]) -> frozenset[str]:
    def read(path: str) -> bytes:
        source = root / path
        # Public checkout inventories must not be satisfied through links or stale bytecode.
        for component in (source, *source.parents):
            if component == root:
                break
            metadata = component.lstat()
            if component.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise CompletenessError("required source contains a link or reparse point")
        if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
            raise CompletenessError("required source is unavailable or outside the bound")
        return source.read_bytes()

    return required_paths(tracked, read)


SMOKE_PROGRAM = r"""
import importlib.util, json, os, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
sys.dont_write_bytecode = True
def guard(event, args):
    forbidden = {
        "subprocess.Popen", "os.system", "os.putenv", "os.mkdir",
        "os.remove", "os.rename", "os.rmdir",
    }
    if event.startswith("socket.") or event in forbidden:
        raise RuntimeError("product smoke side effect refused")
    if event == "open":
        mode, flags = args[1], args[2]
        writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                or (isinstance(flags, int) and flags & writes)):
            raise RuntimeError("product smoke write refused")
sys.addaudithook(guard)
spec = importlib.util.spec_from_file_location("public_product", root / "__init__.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import comfyui_h3_context as product
if Path(product.__file__).resolve() != root / "comfyui_h3_context/__init__.py":
    raise RuntimeError("product import escaped artifact")
if (not module.NODE_CLASS_MAPPINGS
        or set(module.NODE_CLASS_MAPPINGS) != set(module.NODE_DISPLAY_NAME_MAPPINGS)):
    raise RuntimeError("product registration unavailable")
if not (root / module.WEB_DIRECTORY / "h3-context-sidebar.js").is_file():
    raise RuntimeError("frontend runtime unavailable")
sentinel = object()
nodes, names = {"foreign": sentinel}, {"foreign": "Foreign"}
product.register_nodes(nodes, names)
product.register_nodes(nodes, names)
if nodes["foreign"] is not sentinel or set(nodes) != {"foreign", *module.NODE_CLASS_MAPPINGS}:
    raise RuntimeError("product registration changed foreign state")
product.get_public_manifest()
from comfyui_h3_context.core.public_manifest import DEFAULT_WORKFLOW_FIXTURES
for fixture in DEFAULT_WORKFLOW_FIXTURES:
    payload = json.loads((root / fixture.path).read_bytes())
    if not isinstance(payload, dict):
        raise RuntimeError("workflow resource unavailable")
from comfyui_h3_context.core.installation_profiles import load_installation_profiles
from comfyui_h3_context.core.prompt_model_provider import (
    load_prompt_model_catalog, load_legacy_prompt_model_catalog,
)
from comfyui_h3_context.core.semantic_proposal_producer import load_semantic_provider_catalog
from comfyui_h3_context.adapters.comfyui_generation_profile import _load_official_asset_manifest
from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
load_installation_profiles()
load_prompt_model_catalog()
load_legacy_prompt_model_catalog()
load_semantic_provider_catalog()
_load_official_asset_manifest()
fonts = load_packaged_font_manifest()
for font in fonts.faces:
    font.read_verified_bytes()
resources = list((root / "comfyui_h3_context/contracts").glob("*.json"))
for path in resources:
    json.loads(path.read_bytes())
for directory in ("workflows", "subgraphs"):
    for path in (root / directory).rglob("*.json"):
        json.loads(path.read_bytes())
print(json.dumps({
    "status": "PASS", "registered_count": len(module.NODE_CLASS_MAPPINGS),
    "contract_count": len(resources), "checks": [
        "root_loader", "registration", "public_manifest", "installation_catalog",
        "prompt_catalogs", "semantic_catalog", "official_assets", "packaged_fonts",
        "json_resources",
    ],
}))
"""


def smoke_checkout(root: Path) -> SmokeReport:
    """Use only this artifact and stdlib, without profiles, hosts or provider credentials."""
    root = root.resolve()
    environment = {key: value for key, value in os.environ.items() if key.upper() == "SYSTEMROOT"}
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", SMOKE_PROGRAM, str(root)],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompletenessError("isolated product smoke unavailable or timed out") from exc
    if result.returncode != 0:
        # Never echo artifact-produced output: failures can contain private paths or payloads.
        raise CompletenessError("isolated product smoke failed")
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CompletenessError("isolated product smoke returned an invalid report") from exc
    if (
        not isinstance(report, dict)
        or set(report) != {"status", "registered_count", "contract_count", "checks"}
        or report.get("status") != "PASS"
        or report.get("checks") != list(SMOKE_CHECKS)
    ):
        raise CompletenessError("isolated product smoke did not pass")
    registered, contracts = report["registered_count"], report["contract_count"]
    if (
        type(registered) is not int
        or not 0 < registered <= MAX_FILES
        or type(contracts) is not int
        or not 0 < contracts <= MAX_FILES
    ):
        raise CompletenessError("isolated product smoke returned invalid counts")
    return SmokeReport(
        status="PASS",
        registered_count=registered,
        contract_count=contracts,
        checks=list(SMOKE_CHECKS),
    )


def smoke_archive(archive_path: Path, root: Path, report: dict[str, Any]) -> SmokeReport:
    """Extract only already audited member bytes into an owned, fresh temporary root."""
    import hashlib
    import zipfile

    scratch = root / ".tmp"
    scratch.mkdir(exist_ok=True)
    if scratch.is_symlink() or getattr(scratch.lstat(), "st_file_attributes", 0) & 0x400:
        raise CompletenessError("product smoke output contains a link or reparse point")
    with tempfile.TemporaryDirectory(prefix="product-smoke-", dir=scratch) as directory:
        extracted = Path(directory)
        with zipfile.ZipFile(archive_path) as archive:
            if (
                "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
                != report["archive_sha256"]
            ):
                raise CompletenessError("audited archive changed before product smoke")
            for entry in report["entries"]:
                path = _path(entry["path"])
                payload = archive.read(path)
                if (
                    len(payload) != entry["size"]
                    or "sha256:" + hashlib.sha256(payload).hexdigest() != entry["sha256"]
                ):
                    raise CompletenessError("audited archive member changed before product smoke")
                target = extracted / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
        return smoke_checkout(extracted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = smoke_checkout(args.source_root)
    except (OSError, ValueError):
        print("Public product completeness: FAIL")
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
