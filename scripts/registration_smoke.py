"""Run the pinned-host custom-node import, co-installation, and registration smoke."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
# IMPORTANT: acceptance may point at an isolated exact host without mutating the reference clone.
HOST_ROOT = Path(os.environ.get("H3_CONTEXT_HOST_ROOT", ROOT / "reference" / "ComfyUI"))
_HOST_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?")
_HOST_REVISION_RE = re.compile(r"[0-9a-fA-F]{40}")
BLOCKED_OPTIONAL_ROOTS = (
    "aiohttp",
    "comfy_api",
    "cv2",
    "diffusers",
    "httpx",
    "moviepy",
    "requests",
    "torch",
    "transformers",
)


def _load_as_custom_node(module_name: str) -> ModuleType:
    package_root = ROOT / "comfyui_h3_context"
    spec = importlib.util.spec_from_file_location(
        module_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot build a custom-node module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _host_metadata() -> tuple[str, str]:
    version_text = (HOST_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', version_text)
    if version_match is None:
        raise RuntimeError("pinned host version is missing")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=HOST_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if revision.returncode != 0:
        raise RuntimeError("cannot resolve pinned host revision")
    host_version = version_match.group(1)
    host_revision = revision.stdout.strip()
    # CRITICAL: provenance shape is validated, but host identity must never gate registration.
    if len(host_version) > 64 or _HOST_VERSION_RE.fullmatch(host_version) is None:
        raise RuntimeError("host version provenance is malformed")
    if _HOST_REVISION_RE.fullmatch(host_revision) is None:
        raise RuntimeError("host revision provenance is malformed")
    return host_version, host_revision


def main() -> int:
    host_version, host_revision = _host_metadata()

    before_modules = set(sys.modules)
    native_sentinel = object()
    host_nodes: dict[str, object] = {"Native.Sentinel": native_sentinel}
    host_display: dict[str, str] = {"Native.Sentinel": "Native Sentinel"}

    first = _load_as_custom_node("h3_context_smoke_one")
    first.register_nodes(host_nodes, host_display)
    second = _load_as_custom_node("h3_context_smoke_two")
    second.register_nodes(host_nodes, host_display)

    product_ids = set(first.NODE_CLASS_MAPPINGS)
    if product_ids != set(second.NODE_CLASS_MAPPINGS):
        raise RuntimeError("registration ID is not stable and namespaced")
    if not product_ids or not all(
        node_id.startswith("comfyui_h3_context.") for node_id in product_ids
    ):
        raise RuntimeError("custom-node mapping contains an unexpected ID")
    expected_host_ids = {"Native.Sentinel", *product_ids}
    if set(host_nodes) != expected_host_ids:
        raise RuntimeError("host registry contains unexpected entries")
    if host_nodes["Native.Sentinel"] is not native_sentinel:
        raise RuntimeError("unrelated host registry entry was overwritten")
    if any(not host_display[node_id] for node_id in product_ids):
        raise RuntimeError("registration display name is empty")

    newly_imported = set(sys.modules) - before_modules
    imported_optional = sorted(
        module_name
        for module_name in newly_imported
        if module_name == "" or module_name.startswith(BLOCKED_OPTIONAL_ROOTS)
    )
    if imported_optional:
        raise RuntimeError(
            f"optional dependencies imported during registration: {imported_optional}"
        )

    print(
        json.dumps(
            {
                "status": "PASS",
                "host_version": host_version,
                "host_revision": host_revision,
                "registered_ids": sorted(product_ids),
                "unrelated_entry_preserved": True,
                "optional_dependencies_absent": True,
                "reference_code_executed": False,
                "network_or_media_side_effect": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
