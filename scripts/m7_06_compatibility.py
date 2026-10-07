"""Read-only M7-06 compatibility probe and fail-closed matrix evaluator.

The probe reads host metadata and package distribution metadata only. It never installs packages,
imports optional runtimes, changes a host checkout, or starts a ComfyUI process. Registration and
workflow behavior remain covered by ``registration_smoke.py`` and ``m3_08_host_e2e.py``.
"""

from __future__ import annotations

import argparse
import json
import platform as platform_module
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from comfyui_h3_context.core import (
    CompatibilityAssessment,
    CompatibilityDiagnostic,
    CompatibilityObservation,
    CompatibilityStatus,
    CompatibilityVersion,
    PlatformFamily,
    assess_compatibility,
    default_compatibility_matrix,
)
from comfyui_h3_context.core.errors import CompatibilityError

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCE = "comfy_extras/nodes_minimax_h3.py"
NATIVE_NODE_IDS = (
    "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo",
)
DEPENDENCY_NAMES = ("comfy-aimdo", "comfy-kitchen", "torch", "transformers")


def _platform_family(value: str) -> PlatformFamily:
    normalized = value.casefold().replace("-", "_")
    if normalized in {"win", "windows"}:
        return PlatformFamily.WINDOWS
    if normalized in {"linux_wsl", "wsl"}:
        return PlatformFamily.LINUX_WSL
    if normalized == "linux":
        return PlatformFamily.LINUX
    if normalized in {"darwin", "macos", "mac_os"}:
        return PlatformFamily.MACOS
    raise CompatibilityError(
        "invalid_platform", "platform must be windows, linux, linux_wsl, or macos"
    )


def _version(value: str) -> CompatibilityVersion:
    return CompatibilityVersion.from_wire(value)


def _host_metadata(host_root: Path) -> tuple[CompatibilityVersion, str, tuple[str, ...]]:
    """Read host version, Git revision, and native class IDs without executing host code."""

    if not host_root.is_dir() or not (host_root / "main.py").is_file():
        raise CompatibilityError("host_unavailable", "selected host root is not a ComfyUI checkout")
    pyproject = (host_root / "pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'(?m)^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', pyproject)
    if version_match is None:
        raise CompatibilityError("host_version_missing", "host version metadata is missing")
    revision_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=host_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if (
        revision_result.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}\n?", revision_result.stdout) is None
    ):
        raise CompatibilityError("host_revision_missing", "host Git revision is unavailable")
    native_path = host_root / NATIVE_SOURCE
    if not native_path.is_file():
        raise CompatibilityError("native_source_missing", "native MiniMax H3 source is missing")
    native_text = native_path.read_text(encoding="utf-8")
    native_ids = tuple(
        node_id
        for node_id in NATIVE_NODE_IDS
        if re.search(rf"class\s+{re.escape(node_id)}\b", native_text)
    )
    return (
        _version(version_match.group(1)),
        revision_result.stdout.strip(),
        native_ids,
    )


def _distribution_versions(
    interpreter: Path | None = None,
) -> tuple[tuple[str, CompatibilityVersion], ...]:
    if interpreter is not None:
        probe = (
            "import importlib.metadata,json\n"
            f"names={DEPENDENCY_NAMES!r}\n"
            "out={}\n"
            "for name in names:\n"
            "    try:\n"
            "        out[name] = importlib.metadata.version(name)\n"
            "    except importlib.metadata.PackageNotFoundError:\n"
            "        pass\n"
            "print(json.dumps(out))\n"
        )
        result = subprocess.run(
            [str(interpreter), "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise CompatibilityError("dependency_probe_failed", "dependency metadata probe failed")
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CompatibilityError(
                "dependency_probe_invalid", "dependency metadata is invalid"
            ) from exc
        if not isinstance(raw, dict):
            raise CompatibilityError(
                "dependency_probe_invalid", "dependency metadata is not an object"
            )
        values = tuple((str(name), value) for name, value in raw.items())
    else:
        local_values: list[tuple[str, str]] = []
        for name in DEPENDENCY_NAMES:
            try:
                local_values.append((name, metadata.version(name)))
            except metadata.PackageNotFoundError:
                continue
        values = tuple(local_values)

    observed: list[tuple[str, CompatibilityVersion]] = []
    for name, value in values:
        if not isinstance(value, str):
            continue
        try:
            observed.append((name, _version(value)))
        except CompatibilityError:
            # A non-semver distribution version is not silently coerced into support evidence.
            continue
    return tuple(observed)


def _current_platform() -> PlatformFamily:
    if sys.platform.startswith("win"):
        return PlatformFamily.WINDOWS
    if sys.platform == "darwin":
        return PlatformFamily.MACOS
    return (
        PlatformFamily.LINUX_WSL
        if "microsoft" in platform_module.release().casefold()
        else PlatformFamily.LINUX
    )


def build_observation(
    profile_id: str,
    *,
    host_root: Path | None = None,
    host_python: Path | None = None,
    platform_family: PlatformFamily | None = None,
) -> CompatibilityObservation:
    """Collect bounded local/host metadata without importing optional runtime modules."""

    host_version: CompatibilityVersion | None = None
    host_revision: str | None = None
    native_node_ids: tuple[str, ...] = ()
    if host_root is not None:
        host_version, host_revision, native_node_ids = _host_metadata(host_root)

    interpreter = host_python or Path(sys.executable)
    if not interpreter.is_file():
        raise CompatibilityError("interpreter_unavailable", "selected interpreter is unavailable")
    selected_platform = platform_family or _current_platform()
    if host_python is None:
        python_version = CompatibilityVersion(*sys.version_info[:3])
    else:
        probe = (
            "import json,platform,sys; "
            "print(json.dumps({'version': list(sys.version_info[:3]), "
            "'system': platform.system()}))"
        )
        result = subprocess.run(
            [str(interpreter), "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise CompatibilityError(
                "interpreter_probe_failed", "selected interpreter probe failed"
            )
        try:
            payload = json.loads(result.stdout)
            version_parts = payload["version"]
            python_version = CompatibilityVersion(*[int(part) for part in version_parts])
            selected_platform = _platform_family(str(payload["system"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CompatibilityError(
                "interpreter_probe_invalid", "interpreter probe returned invalid metadata"
            ) from exc

    return CompatibilityObservation(
        profile_id=profile_id,
        platform=selected_platform,
        python_version=python_version,
        host_version=host_version,
        host_revision=host_revision,
        dependencies=_distribution_versions(host_python),
        native_node_ids=native_node_ids,
        optional_imports=(),
        foreign_registration_collisions=(),
    )


def evaluate(
    profile_id: str, *, host_root: Path | None = None, host_python: Path | None = None
) -> CompatibilityAssessment:
    matrix = default_compatibility_matrix()
    if matrix.profile(profile_id) is None:
        return assess_compatibility(
            matrix,
            CompatibilityObservation(
                profile_id=profile_id,
                platform=PlatformFamily.LINUX_WSL,
                python_version=CompatibilityVersion(3, 10, 0),
                host_version=None,
                host_revision=None,
                dependencies=(),
                native_node_ids=(),
                optional_imports=(),
                foreign_registration_collisions=(),
            ),
        )
    return assess_compatibility(
        matrix,
        build_observation(profile_id, host_root=host_root, host_python=host_python),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="declared matrix profile ID")
    parser.add_argument("--host-root", type=Path, help="read-only ComfyUI checkout to inspect")
    parser.add_argument(
        "--host-python", type=Path, help="interpreter to probe without importing optional modules"
    )
    parser.add_argument("--json", action="store_true", help="emit one machine-readable result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assessment = evaluate(args.profile, host_root=args.host_root, host_python=args.host_python)
    except CompatibilityError as exc:
        assessment = CompatibilityAssessment(
            args.profile,
            CompatibilityStatus.BLOCKED,
            (
                (
                    # Keep the public result in the same typed envelope as comparison failures.
                    CompatibilityDiagnostic(exc.code, str(exc)),
                )
            ),
        )
    payload = assessment.to_wire()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return (
        0
        if assessment.status
        in {CompatibilityStatus.SUPPORTED, CompatibilityStatus.VALIDATED_DRIFTED}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
