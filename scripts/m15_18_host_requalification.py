"""Run M15-18 exact host profiles and export clean pinned co-installation sources."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.m3_08_host_e2e import (  # noqa: E402
    EXPECTED_HOST_REVISION,
    EXPECTED_HOST_VERSION,
    LATEST_COMPATIBLE_HOST_REVISION,
    LATEST_COMPATIBLE_HOST_VERSION,
    HostDriverError,
    run_supported_host,
)

TMP_ROOT = (ROOT / ".tmp").resolve()
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_LABELS = frozenset({"openclaw", "doctor"})


@dataclass(frozen=True)
class CoInstallSource:
    label: str
    source: Path
    revision: str


def parse_coinstall_source(value: str) -> CoInstallSource:
    try:
        label, raw = value.split("=", 1)
        source_text, revision = raw.rsplit("@", 1)
    except ValueError as exc:
        raise ValueError("co-install source must be label=path@40-hex-revision") from exc
    source = Path(source_text)
    if (
        label not in SOURCE_LABELS
        or not REVISION_RE.fullmatch(revision)
        or source.is_absolute()
        or not source.parts
        or source.parts[0].lower() != "reference"
        or ".." in source.parts
    ):
        raise ValueError("co-install source is outside the closed reference/OID contract")
    return CoInstallSource(label=label, source=source, revision=revision)


def _safe_member_path(name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError("pinned source archive contains an unsafe path")
    return Path(*pure.parts)


def export_pinned_source(source: CoInstallSource, target: Path) -> dict[str, Any]:
    """Export only one exact Git commit; never copy dirty working-tree bytes."""

    source_root = source.source.resolve()
    target_root = target.resolve()
    if TMP_ROOT not in target_root.parents or target_root.exists():
        raise ValueError("pinned source export target must be a new path under .tmp")
    resolved = subprocess.run(
        ["git", "rev-parse", f"{source.revision}^{{commit}}"],
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if resolved.returncode != 0 or resolved.stdout.strip() != source.revision:
        raise ValueError("co-install source revision is unavailable or not exact")
    archived = subprocess.run(
        ["git", "archive", "--format=tar", source.revision],
        cwd=source_root,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if archived.returncode != 0 or not archived.stdout:
        raise ValueError("co-install source archive failed")
    target_root.mkdir(parents=True)
    file_manifest: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            relative = _safe_member_path(member.name)
            destination = target_root / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("pinned source archive contains a non-regular member")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("pinned source archive member is unreadable")
            payload = extracted.read()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            file_manifest.append((relative.as_posix(), hashlib.sha256(payload).hexdigest()))
    canonical = json.dumps(file_manifest, separators=(",", ":")).encode("utf-8")
    return {
        "status": "PASS",
        "label": source.label,
        "revision": source.revision,
        "working_tree_used": False,
        "archive_sha256": hashlib.sha256(archived.stdout).hexdigest(),
        "file_count": len(file_manifest),
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("supported-b323", "latest-compatible"), required=True)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--host-python", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--coinstall-source", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_args)
    sources = [parse_coinstall_source(value) for value in args.coinstall_source]
    if len({item.label for item in sources}) != len(sources):
        raise SystemExit("duplicate co-installation source label")
    if args.profile == "supported-b323":
        expected_version = EXPECTED_HOST_VERSION
        expected_revision = EXPECTED_HOST_REVISION
        native_profile = "supported-b323"
        authority = "supported_b323"
    else:
        expected_version = LATEST_COMPATIBLE_HOST_VERSION
        expected_revision = LATEST_COMPATIBLE_HOST_REVISION
        native_profile = "latest-compatible"
        authority = "latest_compatible_observation"
        if {item.label for item in sources} != SOURCE_LABELS:
            raise SystemExit("latest-compatible M15-18 requires exact OpenClaw and Doctor sources")

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="m15-18-coinstall-", dir=TMP_ROOT) as raw_temp:
        export_root = Path(raw_temp)
        exports: list[tuple[str, Path, dict[str, Any]]] = []
        for source in sources:
            resolved = CoInstallSource(
                source.label,
                (ROOT / source.source).resolve(),
                source.revision,
            )
            target = export_root / source.label
            evidence = export_pinned_source(resolved, target)
            exports.append((source.label, target, evidence))
        try:
            result = run_supported_host(
                host_python=args.host_python,
                host_root=args.host_root,
                timeout=args.timeout,
                modes=("product_shell_base", "product_shell_reference"),
                artifact_path=args.artifact,
                exact_command=(sys.executable, "scripts/m15_18_host_requalification.py", *raw_args),
                browser_e2e=True,
                expected_host_version=expected_version,
                expected_host_revision=expected_revision,
                native_source_profile=native_profile,
                host_authority=authority,
                coinstall_exports=tuple(exports),
            )
        except (HostDriverError, ValueError) as exc:
            result = {
                "schema": "h3-context-m15-18-host-requalification/1",
                "status": "FAIL",
                "profile": args.profile,
                "reason": str(exc),
                "cleanup": "delegated_host_runner",
            }
    result["m15_18_profile"] = args.profile
    result["coinstallation_export_cleanup"] = "PASS"
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
