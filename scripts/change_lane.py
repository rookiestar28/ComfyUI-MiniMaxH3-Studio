"""Propose delivery lanes from inert paths and an explicit Git baseline."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.roadmap_registry import (
    DEFAULT_INDEX_PATH,
    LANES,
    RoadmapIndexError,
    _read_bytes,
    _safe_relative,
    load_index,
)

HEAVY_PREFIXES = (
    "comfyui_h3_context/adapters/",
    "comfyui_h3_context/contracts/",
    "governance/contracts/",
    "frontend/src/contracts/",
    "comfyui_h3_context/core/prompt_model_dialects/",
    "scripts/registry_",
    "scripts/release_",
)

HEAVY_PATTERNS = (
    "comfyui_h3_context/*nodes.py",
    "comfyui_h3_context/core/*provider*.py",
    "comfyui_h3_context/core/workflow_migration*.py",
    "comfyui_h3_context/core/node_contracts*.py",
)

HEAVY_EXACT = (
    "comfyui_h3_context/core/authoring_output_protocol.py",
    "comfyui_h3_context/core/capability_manifest.py",
    "comfyui_h3_context/core/composition_contract.py",
    "comfyui_h3_context/core/context_reporting.py",
    "comfyui_h3_context/core/drift_process.py",
    "comfyui_h3_context/core/durable_workspace_state.py",
    "comfyui_h3_context/core/host_seam_contract.py",
    "comfyui_h3_context/core/managed_generation_publication.py",
    "comfyui_h3_context/core/managed_run.py",
    "comfyui_h3_context/core/managed_run_machine.py",
    "comfyui_h3_context/core/managed_run_release.py",
    "comfyui_h3_context/core/model_manifest.py",
    "comfyui_h3_context/core/private_storage_layout.py",
    "comfyui_h3_context/core/prompt_fidelity.py",
    "comfyui_h3_context/core/public_api.py",
    "comfyui_h3_context/core/public_manifest.py",
    "comfyui_h3_context/core/public_manifest_v2.py",
    "comfyui_h3_context/core/remote_prompt_model.py",
    "comfyui_h3_context/core/security.py",
    "comfyui_h3_context/core/semantic_graph_wire.py",
    "comfyui_h3_context/core/source_profiled_prompt.py",
    "comfyui_h3_context/public_api.py",
    "scripts/acceptance_baseline.py",
    "scripts/browser_ci.py",
    "scripts/browser_ci_evidence.py",
    "scripts/build_gate_report.py",
    "scripts/build_provenance.py",
    "scripts/change_lane.py",
    "scripts/ci_preflight.py",
    "scripts/closeout_matrix.py",
    "scripts/derived_artifacts.py",
    "scripts/drift_process.py",
    "scripts/frontend_build_report.py",
    "scripts/gate_backend.py",
    "scripts/gate_link_receipt.py",
    "scripts/gate_stages.py",
    "scripts/governance/relocate_packaged_artifacts.py",
    "scripts/m15_12_open_box_contract.py",
    "scripts/m15_18_host_requalification.py",
    "scripts/m16_03_release_audit.py",
    "scripts/m17_10_supported_host_smoke.py",
    "scripts/m19_07_temporal_qualification.py",
    "scripts/m19_08_build_matrix.py",
    "scripts/m19_closeout.py",
    "scripts/m22_15_anthropic_qualification.py",
    "scripts/m25_16_service_loopback.py",
    "scripts/m3_08_host_e2e.py",
    "scripts/m7_06_compatibility.py",
    "scripts/managed_run_stategraph.py",
    "scripts/native_t2va_structure.py",
    "scripts/public_consumer_compatibility.py",
    "scripts/nle_hardening_dependency_audit.py",
    "scripts/nle_semantic_browser_media.py",
    "scripts/nle_semantic_conformance.py",
    "scripts/nle_semantic_render.py",
    "scripts/packaged_artifact_readership.py",
    "scripts/process_audio_observer.py",
    "scripts/product_completeness.py",
    "scripts/prompt_model_qualification.py",
    "scripts/public_build_backend.py",
    "scripts/public_projection.py",
    "scripts/registration_smoke.py",
    "scripts/registry_payload.py",
    "scripts/registry_publish_guard.py",
    "scripts/registry_release_controller.py",
    "scripts/release_matrix.py",
    "scripts/roadmap_registry.py",
    "scripts/run_full_tests_linux.sh",
    "scripts/run_full_tests_windows.ps1",
    "scripts/security_audit.py",
    "scripts/semantic_parity.py",
    "scripts/supplied_host_lane.py",
    "scripts/supplied_host_session.py",
    "scripts/supply_chain_manifest.py",
    "scripts/tooling_retirement.py",
    "scripts/workspace_link_guard.py",
)

GENERATED_OUTPUTS = (
    "comfyui_h3_context/contracts/build_provenance_v1.json",
    "comfyui_h3_context/contracts/contract_inventory_v1.json",
    "comfyui_h3_context/contracts/cross_language_surface_v1.json",
    "comfyui_h3_context/core/__init__.py",
    "comfyui_h3_context/web/h3-context-sidebar.js",
    "frontend/src/contracts/generatedDurationResolution.ts",
    "frontend/src/contracts/generatedSurface.ts",
    "frontend/src/i18n/generatedBackendIdentities.ts",
    "governance/contracts/architecture_fitness_v1.json",
    "governance/contracts/architecture_inventory_v1.json",
    "governance/contracts/authoring_output_v1.schema.json",
    "governance/contracts/authoring_render_job_request_v1.schema.json",
    "governance/contracts/authoring_render_receipt_v1.schema.json",
    "governance/contracts/closeout_matrix_v1.json",
    "governance/contracts/fingerprint_domain_v1.json",
    "governance/contracts/m19_closeout_v1.json",
    "governance/contracts/managed_run_stategraph_v1.json",
    "governance/contracts/nle_hardening_coverage_manifest_v1.json",
    "governance/contracts/nle_semantic_conformance_manifest_v1.json",
    "governance/contracts/node_surface_v1.json",
    "governance/contracts/packaged_artifact_readership_v1.json",
    "governance/contracts/public_surface_v1.json",
    "governance/contracts/retirement_eligibility_v1.json",
    "governance/contracts/sbom.spdx.json",
    "governance/contracts/supply_chain_v1.json",
    "public-source-inventory.json",
    "tests/fixtures/canonical_cross_language_vectors.json",
    "tests/fixtures/m16_01_source_requalification.json",
    "tests/fixtures/m16_02_reconstruction_regression.json",
    "tests/fixtures/m25_16_scene_resolver_parity_v1.json",
)

DEPENDENCY_METADATA = (
    ".pre-commit-config.yaml",
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
    "pyproject.toml",
    "requirements/registry-publish-py310-linux-x86_64.txt",
    "requirements/secret-scan-baseline.json",
)

MARKERS = ("[IMPORTANT]", "[DANGEROUS]")
SCHEMA = "h3-context-change-lane/1"


class LaneInputError(ValueError):
    """Raised when a requested source input cannot be read safely."""


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise LaneInputError("changed path must be a non-empty Git-relative path")
    parts = value.split("/")
    if value.startswith("/") or ":" in parts[0] or any(part in {"", ".", ".."} for part in parts):
        raise LaneInputError("changed path must remain repository-relative")
    return value


def classify_paths(
    paths: Sequence[str],
    declared_lane: str | None = None,
    marked: bool = False,
) -> dict[str, object]:
    """Return a proposal only; no source, declaration or authority is modified."""
    if declared_lane is not None and declared_lane not in LANES:
        raise LaneInputError("declared lane is unavailable")
    if not isinstance(marked, bool):
        raise LaneInputError("marked metadata must be boolean")
    unique = sorted({_path(path) for path in paths})
    reasons: list[str] = []
    heavy = declared_lane == "heavy" or marked
    if declared_lane == "heavy":
        reasons.append("declared heavy lane")
    if marked:
        reasons.append("explicit review marker")
    for path in unique:
        if (
            path in HEAVY_EXACT
            or path.startswith(HEAVY_PREFIXES)
            or any(fnmatch.fnmatchcase(path, pattern) for pattern in HEAVY_PATTERNS)
        ):
            heavy = True
            reasons.append("pinned risk surface: " + path)
    excluded = any(path in GENERATED_OUTPUTS or path in DEPENDENCY_METADATA for path in unique)
    if heavy:
        lane = "heavy"
    elif not unique or excluded or len(unique) > 5:
        lane = "standard"
        if excluded:
            reasons.append("generated output or dependency metadata")
        if len(unique) > 5:
            reasons.append("more than five unique changed endpoints")
    else:
        lane = "light"
        reasons.append("at most five endpoints outside pinned risk and generated surfaces")
    # CRITICAL: an empty diff must preserve a declared/marked heavy lane; absence of paths
    # does not remove the item's explicit review or compatibility invariant.
    return {
        "schema": SCHEMA,
        "status": "PROPOSED" if unique else "NO_CHANGE",
        "lane": lane,
        "paths": unique,
        "reasons": reasons,
    }


def _git(root: Path, *arguments: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    run = subprocess.run(
        [
            "git",
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *arguments,
        ],
        capture_output=True,
        check=False,
        env=env,
    )
    if run.returncode != 0:
        raise LaneInputError("explicit Git input is unavailable")
    return run.stdout


def _changed_endpoints(raw: bytes) -> list[str]:
    if not raw:
        return []
    if not raw.endswith(b"\x00"):
        raise LaneInputError("Git name/status output is incomplete")
    fields = raw[:-1].split(b"\x00")
    paths: list[str] = []
    cursor = 0
    while cursor < len(fields):
        status = fields[cursor].decode("ascii")
        cursor += 1
        if not re.fullmatch(r"[ACDMRTUXB][0-9]*", status):
            raise LaneInputError("Git change status is unsupported")
        count = 2 if status[0] in {"R", "C"} else 1
        if cursor + count > len(fields):
            raise LaneInputError("Git rename endpoints are incomplete")
        paths.extend(_path(field.decode("utf-8")) for field in fields[cursor : cursor + count])
        cursor += count
    return paths


def _metadata(root: Path, item_id: str | None, index_path: str | None) -> tuple[str | None, bool]:
    if item_id is None:
        if index_path is not None:
            raise LaneInputError("--index requires an explicit --item")
        return None, False
    relative = _safe_relative(index_path or DEFAULT_INDEX_PATH, "metadata index")
    index = load_index(root, relative)
    rows = cast(list[dict[str, object]], index["items"])
    matches = [row for row in rows if isinstance(row, dict) and row["id"] == item_id]
    if len(matches) != 1:
        raise LaneInputError("requested item metadata is unavailable")
    row = matches[0]
    marked = any(marker in str(row["title"]) for marker in MARKERS)
    if row["plan_path"] is not None:
        plan_path = _safe_relative(str(row["plan_path"]), "item plan")
        plan = _read_bytes(root, plan_path, "item plan").decode("utf-8")
        marked = marked or any(marker in plan for marker in MARKERS)
    return str(row["lane"]), marked


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--item")
    parser.add_argument("--index")
    args = parser.parse_args(argv)
    try:
        cwd = Path.cwd()
        root = Path(_git(cwd, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        base = (
            _git(root, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
            .decode("ascii")
            .strip()
        )
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
            raise LaneInputError("baseline is not a resolved commit")
        head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        # CRITICAL: keep all three views and both rename endpoints. A staged change can be
        # reversed in the worktree, and a rename away from a risk surface must retain that risk.
        options = ("--name-status", "-z", "--no-ext-diff", "--no-textconv", "--find-renames")
        paths: list[str] = []
        for arguments in (
            (*options, base, head, "--"),
            (*options, "--cached", "--"),
            (*options, "--"),
        ):
            paths.extend(_changed_endpoints(_git(root, "diff", *arguments)))
        raw_untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        if raw_untracked:
            if not raw_untracked.endswith(b"\x00"):
                raise LaneInputError("Git untracked output is incomplete")
            paths.extend(_path(path.decode("utf-8")) for path in raw_untracked[:-1].split(b"\x00"))
        declared, marked = _metadata(root, args.item, args.index)
        result = {**classify_paths(paths, declared, marked), "base": base}
    except (LaneInputError, RoadmapIndexError, OSError, UnicodeError) as exc:
        # Error categories stay content-free: Git stderr and private metadata are not printed.
        print("CHANGE LANE: REFUSED: " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
