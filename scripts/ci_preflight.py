"""Validate cheap CI prerequisites and collection without replaying the browser matrix."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PYTHON_ENV = "H3_CONTEXT_E2E_PYTHON"


def fixture_python(root: Path, *, environ: dict[str, str] | None = None) -> Path:
    """Select only the active OS-specific venv inside this checkout."""
    root = root.resolve()
    environment = os.environ if environ is None else environ
    names = (".venv",) if sys.platform == "win32" else (".venv", ".venv-wsl")
    relative = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    active = Path(sys.prefix).absolute()
    candidates = {root / name / relative for name in names}
    selected = Path(environment.get(PYTHON_ENV, str(active / relative))).absolute()
    prefix = selected.parent.parent
    # IMPORTANT: venv executables may link to their base Python on Linux; the venv directory
    # and active prefix must stay here. Resolving only the executable would reject real venvs.
    if (
        selected not in candidates
        or prefix.resolve() != prefix
        or active.resolve() != prefix
        or not (prefix / "pyvenv.cfg").is_file()
        or not selected.is_file()
    ):
        raise ValueError(
            "use the current project's project-local Python (.venv, or Linux .venv-wsl); "
            "install .[dev,host-tests] with that interpreter; no global or escaping override"
        )
    return selected


def validate_workflow(workflow: dict[str, Any], package_manager: str = "pnpm@11.3.0") -> None:
    """Check bootstrap ordering against the actual jobs, not prose or step names."""
    try:
        jobs = workflow["jobs"]
        node_versions = []
        for name in ("frontend", "backend", "browser"):
            steps = jobs[name]["steps"]
            node = next(
                i for i, row in enumerate(steps) if "actions/setup-node@" in row.get("uses", "")
            )
            node_versions.append(steps[node]["with"]["node-version"])
            pnpm = next(
                i
                for i, row in enumerate(steps)
                if f"corepack prepare {package_manager} --activate" in row.get("run", "")
            )
            frozen = next(
                i
                for i, row in enumerate(steps)
                if row.get("run") == "pnpm --dir frontend install --frozen-lockfile"
            )
            consumer = next(
                i
                for i, row in enumerate(steps)
                if (
                    "scripts/browser_ci.py" in row.get("run", "")
                    if name == "browser"
                    else "-m pytest" in row.get("run", "")
                    if name == "backend"
                    else "pnpm --dir frontend run check" == row.get("run")
                )
            )
            if not node < pnpm < frozen < consumer:
                raise ValueError("frontend bootstrap ordering")
            if name != "frontend":
                python = next(
                    i
                    for i, row in enumerate(steps)
                    if "actions/setup-python@" in row.get("uses", "")
                )
                install = next(
                    i
                    for i, row in enumerate(steps)
                    if '-e ".[dev,host-tests]"' in row.get("run", "")
                )
                if (
                    steps[python]["with"]["python-version"] != "3.10"
                    or not python < install < consumer
                ):
                    raise ValueError("Python bootstrap ordering/version")
                if name == "browser":
                    paths = {row["python"] for row in jobs[name]["strategy"]["matrix"]["include"]}
                    if paths != {".venv/bin/python", ".venv/Scripts/python.exe"}:
                        raise ValueError("browser interpreter matrix")
                    if (
                        "python -m venv .venv" not in steps[install]["run"]
                        or "${{ matrix.python }} -m pip install" not in steps[install]["run"]
                    ):
                        raise ValueError("browser interpreter bootstrap")
                    if steps[consumer]["run"] != "${{ matrix.python }} scripts/browser_ci.py":
                        raise ValueError("browser consumer interpreter")
                else:
                    minimum = next(
                        i
                        for i, row in enumerate(steps)
                        if row.get("run") == "python scripts/ci_preflight.py --public-minimum"
                    )
                    cpu = next(
                        i
                        for i, row in enumerate(steps)
                        if "torch==2.14.0+cpu" in row.get("run", "")
                    )
                    process = next(
                        i
                        for i, row in enumerate(steps)
                        if "Pillow==12.3.0 psutil==7.2.2" in row.get("run", "")
                    )
                    if not max(install, frozen, cpu, process) < minimum < consumer:
                        raise ValueError("public minimum bootstrap must precede full pytest")
        if len(set(node_versions)) != 1 or not re.fullmatch(r"24\.\d+\.\d+", node_versions[0]):
            raise ValueError("Node bootstrap identity")
    except (KeyError, TypeError, StopIteration, ValueError) as error:
        raise ValueError(f"CI bootstrap contract invalid: {error}") from error


def checked(args: list[str], root: Path, *, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args, cwd=root, env=env, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def runtime_preflight(root: Path) -> dict[str, str]:
    import yaml

    python = fixture_python(root)
    package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    manager = package["packageManager"]
    if not re.fullmatch(r"pnpm@\d+\.\d+\.\d+", manager):
        raise ValueError("packageManager must pin pnpm exactly")
    validate_workflow(yaml.safe_load((root / ".github/workflows/ci.yml").read_text()), manager)
    node = checked(["node", "-p", "process.versions.node"], root)
    pnpm = checked(["pnpm.cmd" if sys.platform == "win32" else "pnpm", "--version"], root)
    if node.split(".")[0] != "24" or pnpm != manager.removeprefix("pnpm@"):
        raise ValueError(
            f"require supported Node 24 and exact {manager}; found Node {node}, pnpm {pnpm}"
        )
    checked(
        [
            str(python),
            "-c",
            "import aiohttp; import scripts.m25_16_timeline_fixture; "
            "import scripts.m25_16_import_fixture; import scripts.m25_16_service_loopback",
        ],
        root,
    )
    env = dict(os.environ)
    env[PYTHON_ENV] = str(python)
    print(
        f"Local prerequisites: Python {sys.version.split()[0]}, Node {node}, pnpm {pnpm}; "
        "supported local runtime, NOT minimum-runtime/public proof",
        flush=True,
    )
    return env


def browser_collection(root: Path, env: dict[str, str]) -> None:
    from scripts import browser_ci, gate_stages

    environment = browser_ci.collection_environment(env)
    broad = browser_ci.collect("playwright.config.ts", environment, root=root)
    portable = browser_ci.collect(browser_ci.PROFILE, environment, root=root)
    native = browser_ci.native_cases(root)
    browser_ci.validate_partition(broad, portable, native)
    args = [
        *browser_ci.command("playwright.config.ts"),
        *gate_stages.SMOKE_SPECS,
        "--grep",
        r"(?:^|\s)(?:" + "|".join(re.escape(title) for _, title in gate_stages.SMOKE_CASES) + ")$",
        "--list",
        "--reporter=json",
    ]
    smoke = browser_ci.collected(json.loads(checked(args, root, env=environment)))
    selected = Counter((file, title) for file, title in gate_stages.SMOKE_CASES)
    matched: Counter[tuple[str, str]] = Counter()
    for file, full_title in smoke:
        matches = [
            key
            for key in selected
            if key[0] == file and (full_title == key[1] or full_title.endswith(" " + key[1]))
        ]
        if len(matches) != 1:
            raise ValueError("smoke identity is missing or ambiguous")
        matched[matches[0]] += smoke[(file, full_title)]
    if matched != selected or smoke - portable:
        raise ValueError("smoke does not preserve the exact portable subset")
    print(
        f"Browser collection: {sum(broad.values())} broad = {sum(portable.values())} portable + "
        f"{sum(native.values())} native NOT_RUN; smoke subset {sum(smoke.values())}; "
        "browser execution NOT_RUN",
        flush=True,
    )


def require_minimum_python(version: tuple[int, int]) -> None:
    if version != (3, 10):
        raise ValueError("public minimum-runtime proof requires actual Python 3.10")


def validate_public_checkout(root: Path) -> None:
    from scripts.public_source_policy import allowed

    if Path(checked(["git", "rev-parse", "--show-toplevel"], root)).resolve() != root.resolve():
        raise ValueError("public proof must own its checkout, not inherit a parent repository")
    if checked(["git", "rev-list", "--count", "--all"], root) != "1":
        raise ValueError("public minimum-runtime proof requires a single-commit checkout")
    tracked = [path for path in checked(["git", "ls-files", "-z"], root).split("\0") if path]
    if not tracked or any(not allowed(path) for path in tracked):
        raise ValueError("public checkout contains a non-public tracked path")
    for name in (".planning", "reference", "REFERENCE", ".reference", "ROADMAP.md", "AGENTS.md"):
        if (root / name).exists():
            raise ValueError("public checkout contains private repository records")
    if checked(["git", "ls-files", "-ci", "--exclude-standard"], root):
        raise ValueError("public checkout tracks an ignored path")
    untracked = checked(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    if untracked:
        raise ValueError("public checkout has uncommitted public source inputs")
    if checked(["git", "diff", "HEAD", "--name-only"], root):
        raise ValueError("public checkout source differs from its single commit")


def minimum_command(root: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--capture=sys",
        "--basetemp",
        str(root / ".tmp/ci-public-minimum"),
        "tests/test_native_t2va_structure.py",
        "tests/test_registry_publish_guard.py::RegistryPublishGuardTests",
        "tests/test_m19_closeout.py::GeneratedMatrixTests",
        "tests/test_m19_closeout.py::HistoricalReplayTests",
        "tests/test_m19_closeout.py::SyntheticHistoryTests",
        "tests/test_closeout_matrix.py::GeneratedMatrixTests",
        "tests/test_closeout_matrix.py::HistoricalReplayTests",
        "tests/test_closeout_matrix.py::SyntheticHistoryTests",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-minimum", action="store_true")
    args = parser.parse_args()
    try:
        if args.public_minimum:
            require_minimum_python(sys.version_info[:2])
            validate_public_checkout(ROOT)
            checked(
                [
                    sys.executable,
                    "-c",
                    "import comfyui_h3_context; "
                    "from scripts.nle_hardening_dependency_audit import tomllib; "
                    "tomllib.loads('[proof]\\nminimum = true'); "
                    "import scripts.registry_publish_guard",
                ],
                ROOT,
            )
            subprocess.run(minimum_command(ROOT), cwd=ROOT, check=True)
            print(
                "Public Python 3.10 bounded contracts: PASS; "
                "private historical replay NOT_RUN; full backend NOT_RUN"
            )
        else:
            browser_collection(ROOT, runtime_preflight(ROOT))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"CI preflight: FAIL ({type(error).__name__}: {error})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
