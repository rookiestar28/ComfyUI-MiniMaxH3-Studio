"""Emit bounded machine-readable sdist/direct-wheel/wheel-from-sdist build evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "h3-context-build-gate/1"
STAGE_TIMEOUT_SECONDS = 180
# IMPORTANT: use the canonical Studio distribution; the retired name rejects valid built sdists.
SDIST_ROOT_PATTERN = re.compile(r"^minimax_h3_studio-[0-9]+\.[0-9]+\.[0-9]+$")

JsonObject = dict[str, Any]


class BuildGateReportError(RuntimeError):
    """A bounded build stage or artifact comparison failed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _purge_stale_build_state() -> list[str]:
    """Remove setuptools' incremental staging areas before building from the working tree.

    `build_py` copies a source file into `build/lib/` when it is new or newer, and never removes a
    staged file whose source has since been deleted.  So a wheel built directly from the working
    tree keeps shipping a file the repository no longer contains, for as long as a stale `build/`
    survives on disk -- while the wheel built from a freshly extracted sdist, which starts in a
    clean temporary directory, correctly does not.

    That asymmetry lands squarely in this gate's own `wheel_comparison`, where it reads as
    `only_in_direct`: exactly the signature of a real packaging mismatch.  M18-05 is the first item
    to delete a packaged contract, and it surfaced the confusion immediately -- five deleted schemas
    reappeared in the direct wheel.  Purging first makes the comparison a statement about the
    repository rather than about how recently somebody built on this machine, and it keeps the
    genuine finding the comparison exists for: a file reachable through package data but missing
    from the sdist still shows up as `only_in_direct` after a clean build.

    The removal is deliberately narrow: the two staging directories setuptools itself owns and
    regenerates, both directly beneath the repository root, neither of them tracked.  Anything that
    is not a real directory at exactly that location is left alone.
    """

    removed: list[str] = []
    candidates = [ROOT / "build", *sorted(ROOT.glob("*.egg-info"))]
    for path in candidates:
        resolved = path.resolve()
        if resolved.parent != ROOT or path.is_symlink() or not path.is_dir():
            continue
        shutil.rmtree(resolved)
        removed.append(path.name)
    return removed


def _run_stage(stage_id: str, command: list[str], *, cwd: Path) -> JsonObject:
    started_at = _utc_now()
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT_SECONDS,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {
            "id": stage_id,
            "status": "FAIL",
            "exit_code": None,
            "failure_kind": "timeout",
            "started_at": started_at,
            "ended_at": _utc_now(),
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    except OSError:
        return {
            "id": stage_id,
            "status": "FAIL",
            "exit_code": None,
            "failure_kind": "spawn_error",
            "started_at": started_at,
            "ended_at": _utc_now(),
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    return {
        "id": stage_id,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "exit_code": result.returncode,
        "failure_kind": None if result.returncode == 0 else "nonzero_exit",
        "started_at": started_at,
        "ended_at": _utc_now(),
        "duration_seconds": round(time.monotonic() - started, 6),
    }


def _single_artifact(directory: Path, suffix: str) -> Path:
    candidates = [
        path for path in directory.iterdir() if path.is_file() and path.name.endswith(suffix)
    ]
    if len(candidates) != 1:
        raise BuildGateReportError(f"expected one {suffix} artifact")
    return candidates[0]


def _safe_extract_sdist(sdist: Path, destination: Path) -> Path:
    suffix = ".tar.gz"
    if not sdist.name.endswith(suffix):
        raise BuildGateReportError("sdist filename is unexpected")
    sdist_root = sdist.name[: -len(suffix)]
    if not SDIST_ROOT_PATTERN.fullmatch(sdist_root):
        raise BuildGateReportError("sdist filename has an unexpected source root")
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        normalized = [member.name.replace("\\", "/") for member in members]
        if any(name.startswith("/") or name == ".." or "../" in name for name in normalized):
            raise BuildGateReportError("sdist contains an unsafe path")
        if any(
            member.issym() or member.islnk() or member.isdev() or member.isfifo()
            for member in members
        ):
            raise BuildGateReportError("sdist contains an unsafe member type")
        if {name.split("/", 1)[0] for name in normalized if name} != {sdist_root}:
            raise BuildGateReportError("sdist root is unexpected")
        archive.extractall(destination, members=members, filter="data")
    source = destination / sdist_root
    if not source.is_dir():
        raise BuildGateReportError("sdist extraction did not produce its source root")
    return source


def _wheel_payload(wheel: Path) -> tuple[dict[str, str], dict[str, str]]:
    content: dict[str, str] = {}
    metadata: dict[str, str] = {}
    try:
        with zipfile.ZipFile(wheel) as archive:
            for name in sorted(archive.namelist()):
                if name.endswith("/") or name.endswith(".dist-info/RECORD"):
                    continue
                digest = hashlib.sha256(archive.read(name)).hexdigest()
                if ".dist-info/" in name:
                    metadata[name.rsplit("/", 1)[-1]] = digest
                else:
                    content[name] = digest
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildGateReportError("wheel comparison could not read an artifact") from exc
    return content, metadata


def compare_wheels(direct: Path, from_sdist: Path) -> JsonObject:
    """Compare content and metadata bytes while excluding generated RECORD rows."""

    direct_content, direct_metadata = _wheel_payload(direct)
    sdist_content, sdist_metadata = _wheel_payload(from_sdist)
    direct_names = set(direct_content)
    sdist_names = set(sdist_content)
    common_content = direct_names & sdist_names
    metadata_names = set(direct_metadata) | set(sdist_metadata)
    content_mismatches = sorted(
        name for name in common_content if direct_content[name] != sdist_content[name]
    )
    metadata_mismatches = sorted(
        name for name in metadata_names if direct_metadata.get(name) != sdist_metadata.get(name)
    )
    return {
        "content_files_equal": not (direct_names ^ sdist_names or content_mismatches),
        "metadata_equal": not metadata_mismatches,
        "only_in_direct": sorted(direct_names - sdist_names),
        "only_in_from_sdist": sorted(sdist_names - direct_names),
        "content_mismatches": content_mismatches,
        "metadata_mismatches": metadata_mismatches,
    }


def _artifact_record(kind: str, path: Path) -> JsonObject:
    return {
        "kind": kind,
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _write_report(path: Path, report: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_build_gate(artifact_root: Path, *, report_path: Path) -> JsonObject:
    """Run all build stages once, retain first failure, compare artifacts, and emit JSON."""

    artifact_root = artifact_root.resolve()
    stages: list[JsonObject] = []
    first_failure: JsonObject | None = None
    cleanup_failure: JsonObject | None = None
    extraction_root: Path | None = None
    cleanup_verified = True
    artifacts: list[JsonObject] = []
    comparison: JsonObject = {}
    purged: list[str] = []
    phase = "artifact_root"
    caught_error: BaseException | None = None
    try:
        if artifact_root.exists() and any(artifact_root.iterdir()):
            raise BuildGateReportError("artifact root must be absent or empty")
        artifact_root.mkdir(parents=True, exist_ok=True)
        sdist_dir = artifact_root / "sdist"
        direct_dir = artifact_root / "direct-wheel"
        from_sdist_dir = artifact_root / "wheel-from-sdist"
        for directory in (sdist_dir, direct_dir, from_sdist_dir):
            directory.mkdir()
        # IMPORTANT: keep this short; nested wheel staging can hit legacy Windows path limits.
        extraction_root = Path(tempfile.mkdtemp(prefix="wfs-", dir=artifact_root.parent))
        cleanup_verified = False
        phase = "purge_stale_build_state"
        purged = _purge_stale_build_state()
        commands = [
            (
                "sdist",
                [sys.executable, "-m", "build", "--sdist", "--outdir", str(sdist_dir), str(ROOT)],
                ROOT,
            ),
            (
                "direct_wheel",
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(direct_dir), str(ROOT)],
                ROOT,
            ),
        ]
        for stage_id, command, cwd in commands:
            phase = stage_id
            stage = _run_stage(stage_id, command, cwd=cwd)
            stages.append(stage)
            if stage["status"] != "PASS":
                first_failure = {"stage": stage_id, "kind": stage["failure_kind"]}
                break
        if first_failure is None:
            phase = "artifact_discovery"
            sdist = _single_artifact(sdist_dir, ".tar.gz")
            phase = "sdist_extraction"
            extracted = _safe_extract_sdist(sdist, extraction_root)
            phase = "wheel_from_sdist"
            from_sdist_stage = _run_stage(
                "wheel_from_sdist",
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--outdir",
                    str(from_sdist_dir),
                    str(extracted),
                ],
                cwd=ROOT,
            )
            stages.append(from_sdist_stage)
            if from_sdist_stage["status"] != "PASS":
                first_failure = {
                    "stage": "wheel_from_sdist",
                    "kind": from_sdist_stage["failure_kind"],
                }
            else:
                phase = "artifact_discovery"
                direct = _single_artifact(direct_dir, ".whl")
                from_sdist = _single_artifact(from_sdist_dir, ".whl")
                phase = "wheel_comparison"
                comparison = compare_wheels(direct, from_sdist)
                phase = "artifact_inventory"
                artifacts = [
                    _artifact_record("sdist", sdist),
                    _artifact_record("direct_wheel", direct),
                    _artifact_record("wheel_from_sdist", from_sdist),
                ]
                if not comparison["content_files_equal"] or not comparison["metadata_equal"]:
                    first_failure = {"stage": "wheel_comparison", "kind": "artifact_mismatch"}
    except (BuildGateReportError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        caught_error = exc
        if first_failure is None:
            kind = {
                "artifact_root": "artifact_root_error",
                "artifact_discovery": "artifact_error",
                "sdist_extraction": "artifact_error",
                "wheel_comparison": "artifact_comparison_error",
                "artifact_inventory": "artifact_inventory_error",
            }.get(phase, "stage_error")
            first_failure = {"stage": phase, "kind": kind}
    finally:
        if extraction_root is not None:
            try:
                shutil.rmtree(extraction_root, ignore_errors=False)
                cleanup_verified = not extraction_root.exists()
            except OSError as exc:
                cleanup_verified = False
                cleanup_failure = {"stage": "cleanup", "kind": "cleanup_error"}
                if first_failure is None:
                    first_failure = cleanup_failure
                if caught_error is None:
                    caught_error = exc

    report: JsonObject = {
        "schema": SCHEMA,
        "status": "PASS" if first_failure is None and cleanup_verified else "FAIL",
        "python_version": sys.version.split()[0],
        "build_backend": "setuptools.build_meta",
        "tool_versions": {
            "build": importlib.metadata.version("build"),
            "setuptools": importlib.metadata.version("setuptools"),
            "wheel": importlib.metadata.version("wheel"),
        },
        "stage_timeout_seconds": STAGE_TIMEOUT_SECONDS,
        # Naming what was thrown away keeps the comparison auditable: a reader can tell a clean
        # build from one that inherited somebody's staging directory.
        "purged_build_state": sorted(purged),
        "stages": stages,
        "first_failure": first_failure,
        "artifacts": artifacts,
        "wheel_comparison": comparison,
        "cleanup_verified": cleanup_verified,
        "cleanup_failure": cleanup_failure,
        "network_policy": "build isolation only; no provider or publication operation",
    }
    _write_report(report_path, report)
    if report["status"] != "PASS":
        error = BuildGateReportError("build gate report contains a failed stage")
        if caught_error is None:
            raise error
        raise error from caught_error
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_build_gate(args.artifact_root, report_path=args.report)
    except (BuildGateReportError, OSError) as exc:
        print(f"BUILD GATE REPORT: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
