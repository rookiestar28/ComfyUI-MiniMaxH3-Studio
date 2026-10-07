"""Rebuild the frontend from a frozen offline lockfile and compare shipped runtime bytes."""

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
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: direct script execution must import this exact worktree, not a stale installed package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.safe_paths import (  # noqa: E402
    UnsafePathError,
    ensure_directory,
    prepare_regular_output,
    validate_directory,
    validate_path_components,
    validate_regular_file,
)

FRONTEND_ROOT = ROOT / "frontend"
RUNTIME_BUNDLE = ROOT / "comfyui_h3_context" / "web" / "h3-context-sidebar.js"
TIMEOUT_SECONDS = 180
FRONTEND_BUILD_INPUT_NAMES = (
    "buildMetadata.ts",
    "buildProvenance.ts",
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "vite.config.ts",
)
MAX_FRONTEND_INPUT_ENTRIES = 512
MAX_FRONTEND_INPUT_FILE_BYTES = 1024 * 1024
MAX_FRONTEND_INPUT_TOTAL_BYTES = 16 * 1024 * 1024
JsonObject = dict[str, Any]


class FrontendBuildError(RuntimeError):
    """Raised when the frozen offline frontend rebuild cannot prove exact parity."""


def _module_dependency_boundary(runtime_text: str) -> bool:
    node = shutil.which("node")
    if node is None:
        raise FrontendBuildError("frontend module parser toolchain is unavailable")
    # CRITICAL: parse module syntax with the pinned build toolchain. Textual `from` matching
    # mistakes minified property/string text for imports and misses bare side-effect imports.
    probe = """
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const require = createRequire(resolve('package.json'));
const viteRequire = createRequire(require.resolve('vite'));
const {parseSync} = await import(pathToFileURL(viteRequire.resolve('rolldown/utils')).href);
const parsed = parseSync('runtime.js', readFileSync(0, 'utf8'), {sourceType:'module'});
if (parsed.errors.length) process.exit(2);
const specifiers = parsed.module.staticImports.map(row => row.moduleRequest.value);
for (const row of parsed.module.staticExports)
  for (const entry of row.entries)
    if (entry.moduleRequest !== null) specifiers.push(entry.moduleRequest.value);
console.log(JSON.stringify({
  bare: specifiers.some(value => !['./','../','/'].some(prefix => value.startsWith(prefix))),
  dynamic: parsed.module.dynamicImports.length > 0
}));
"""
    try:
        result = subprocess.run(
            [node, "--input-type=module", "-e", probe],
            cwd=FRONTEND_ROOT,
            input=runtime_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise FrontendBuildError("frontend module parser failed")
        payload = json.loads(result.stdout)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"bare", "dynamic"}
            or not isinstance(payload["bare"], bool)
            or not isinstance(payload["dynamic"], bool)
        ):
            raise FrontendBuildError("frontend module parser response is invalid")
        return bool(payload["bare"] or payload["dynamic"])
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
        raise FrontendBuildError("frontend module parser unavailable or response invalid") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrontendBuildError("frontend build stage failed") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-2_000:]
        raise FrontendBuildError(f"frontend build stage failed: {detail}")


def _validated_frontend_build_inputs() -> tuple[tuple[Path, Path], ...]:
    """Return the closed regular-file build inventory without following descendants."""

    inputs: list[tuple[Path, Path]] = []
    total_bytes = 0
    for name in FRONTEND_BUILD_INPUT_NAMES:
        path = validate_regular_file(
            FRONTEND_ROOT / name,
            maximum_bytes=MAX_FRONTEND_INPUT_FILE_BYTES,
        )
        inputs.append((path, Path(name)))
        total_bytes += path.lstat().st_size
    if total_bytes > MAX_FRONTEND_INPUT_TOTAL_BYTES:
        raise FrontendBuildError("frontend fixed inputs exceed the byte bound")

    source_root = validate_directory(FRONTEND_ROOT / "src")
    directories = [source_root]
    entry_count = len(inputs)
    while directories:
        directory = directories.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            raise FrontendBuildError("frontend source inventory is unavailable") from exc
        for child in children:
            entry_count += 1
            if entry_count > MAX_FRONTEND_INPUT_ENTRIES:
                raise FrontendBuildError("frontend source inventory exceeds the entry bound")
            # CRITICAL: validate descendants before copying; copytree follows Windows
            # junctions.
            candidate, metadata = validate_path_components(child)
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafePathError("frontend source contains a non-regular entry")
            if metadata.st_size < 0 or metadata.st_size > MAX_FRONTEND_INPUT_FILE_BYTES:
                raise FrontendBuildError("frontend source file exceeds the size bound")
            total_bytes += metadata.st_size
            if total_bytes > MAX_FRONTEND_INPUT_TOTAL_BYTES:
                raise FrontendBuildError("frontend source inventory exceeds the byte bound")
            inputs.append((candidate, candidate.relative_to(FRONTEND_ROOT)))
    if len(inputs) == len(FRONTEND_BUILD_INPUT_NAMES):
        raise FrontendBuildError("frontend source inventory is empty")
    return tuple(sorted(inputs, key=lambda item: item[1].as_posix()))


def run_frontend_build_report(output_root: Path) -> JsonObject:
    """Run one bounded temporary offline install/build and return content-free evidence."""

    try:
        output_root = ensure_directory(output_root)
        validate_directory(FRONTEND_ROOT)
        validate_regular_file(RUNTIME_BUNDLE)
        build_inputs = _validated_frontend_build_inputs()
        validate_regular_file(
            ROOT / "comfyui_h3_context" / "contracts" / "build_provenance_v1.json",
            maximum_bytes=32_768,
        )
    except UnsafePathError as exc:
        raise FrontendBuildError(
            "frontend build input is unsafe, linked, reparse, or outside bounds"
        ) from exc
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise FrontendBuildError("pnpm is unavailable")
    package_path = next(path for path, relative in build_inputs if relative == Path("package.json"))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("packageManager") != "pnpm@11.3.0":
        raise FrontendBuildError("frontend package manager pin drifted")
    work = Path(tempfile.mkdtemp(prefix="h3-frontend-", dir=output_root))
    repository_lock_before = _sha256(FRONTEND_ROOT / "pnpm-lock.yaml")
    cleanup_verified = False
    result_report: JsonObject | None = None
    try:
        source = work / "frontend"
        source.mkdir()
        for input_path, relative in build_inputs:
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(input_path, destination, follow_symlinks=False)
        lock_before = _sha256(source / "pnpm-lock.yaml")
        output = work / "output"
        environment = {
            **os.environ,
            "CI": "1",
            "NPM_CONFIG_OFFLINE": "true",
            "PNPM_CONFIG_OFFLINE": "true",
        }
        lock_validation_flags = [
            "--offline",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--lockfile-only",
        ]
        _run(
            [pnpm, "install", *lock_validation_flags],
            cwd=source,
            environment=environment,
        )
        _run(
            [pnpm, "exec", "vite", "build", "--outDir", str(output), "--emptyOutDir"],
            cwd=FRONTEND_ROOT,
            environment=environment,
        )
        lock_after = _sha256(source / "pnpm-lock.yaml")
        entries = sorted(path.name for path in output.iterdir() if path.is_file())
        source_maps = sorted(path.name for path in output.rglob("*.map") if path.is_file())
        if entries != ["h3-context-sidebar.js"] or source_maps:
            raise FrontendBuildError("frontend rebuild emitted an undeclared runtime entry")
        rebuilt = output / "h3-context-sidebar.js"
        rebuilt_bytes = rebuilt.read_bytes()
        try:
            runtime_text = rebuilt_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise FrontendBuildError("frontend runtime is not strict UTF-8") from exc
        bare_module_specifiers = _module_dependency_boundary(runtime_text)
        cdn_dependencies = (
            re.search(
                r"https?://(?:[^/]+\.)?(?:unpkg\.com|jsdelivr\.net|esm\.sh|skypack\.dev)",
                runtime_text,
                re.IGNORECASE,
            )
            is not None
        )
        build_path_markers = "node_modules" in runtime_text.casefold() or any(
            marker in runtime_text for marker in ("//#region", "//#endregion")
        )
        lifecycle_registration_count = runtime_text.count("comfyui-h3-context.product-shell.v1")
        if (
            bare_module_specifiers
            or cdn_dependencies
            or build_path_markers
            or lifecycle_registration_count != 1
        ):
            raise FrontendBuildError("frontend runtime dependency or lifecycle boundary drifted")
        bundle_hash = _sha256(RUNTIME_BUNDLE)
        parity = rebuilt_bytes == RUNTIME_BUNDLE.read_bytes()
        repository_lock_after = _sha256(FRONTEND_ROOT / "pnpm-lock.yaml")
        if repository_lock_before != repository_lock_after or not parity:
            raise FrontendBuildError(
                "frontend lockfile or runtime bundle parity failed: "
                f"source_lock_unchanged={repository_lock_before == repository_lock_after}, "
                f"bundle_parity={parity}, "
                f"rebuilt_sha256={_sha256(rebuilt)}, committed_sha256={bundle_hash}"
            )
        result_report = {
            "bundle_parity": True,
            "bundle_sha256": bundle_hash,
            "bundle_bytes": len(rebuilt_bytes),
            "bare_module_specifiers": bare_module_specifiers,
            "build_path_markers": build_path_markers,
            "cdn_dependencies": cdn_dependencies,
            "cleanup_verified": False,
            "dependency_source": "existing_frozen_node_modules",
            "lock_validation": "offline_frozen_lockfile_only",
            "lock_validation_flags": lock_validation_flags,
            "lockfile_unchanged": True,
            "lifecycle_registration_count": lifecycle_registration_count,
            "network": "disabled",
            "package_manager": "pnpm@11.3.0",
            "runtime_entries": entries,
            "schema": "h3-context-frontend-build/1",
            "source_maps": source_maps,
            "status": "PASS",
            "validation_copy_normalized": lock_before != lock_after,
        }
    finally:
        shutil.rmtree(work, ignore_errors=False)
        cleanup_verified = not work.exists()
    if result_report is None or not cleanup_verified:
        raise FrontendBuildError("frontend build cleanup was not verified")
    result_report["cleanup_verified"] = True
    return result_report


__all__ = ["FrontendBuildError", "run_frontend_build_report"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_frontend_build_report(args.output_root)
        report_path = prepare_regular_output(args.report)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (FrontendBuildError, OSError, UnsafePathError) as exc:
        print(f"FRONTEND BUILD REPORT: FAIL: {exc}")
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
