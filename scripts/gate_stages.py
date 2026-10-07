"""The Full Gate stage table, and the fingerprint that decides whether a stage may be skipped.

Resume is opt-in.  With no ``--resume`` the runners never consult or write the cache, so a gate
invoked the ordinary way behaves exactly as it did before this module existed.

What a resumed PASS means
-------------------------

A stage's recorded PASS is bound to a fingerprint of the inputs that stage reads.  It may be skipped
only when that fingerprint is byte-identical.  So "FULL GATE: PASS" still means every stage passed
against the current content of what it reads -- the same guarantee a single sequential run gives.

Position-based resume -- remember the failed index, restart there -- is deliberately not
implementable through this module.  It would let a stage's PASS survive a change to the code that
stage tested, which is a false-acceptance mechanism however convenient.  There is no flag for it.

Fail-safe by construction
-------------------------

Every stage's fingerprint includes the digest of every tracked file that no *narrow* stage claims
-- narrow meaning a stage declaring something smaller than the whole tree.  An under-declared or
brand-new path therefore invalidates the whole gate rather than silently invalidating nothing.
Measuring that set against every stage instead would make it vacuous, since three stages already
declare the whole tree.  The runner scripts and this file are folded in too, so editing a stage's
command or this table discards all state.

Item M23-02.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PNPM = "pnpm.cmd" if sys.platform == "win32" else "pnpm"

STATE_VERSION = 3
STATE_RELATIVE = Path(".tmp/gate-state.json")
TEMPLATE_CORPUS = "reference/rm02/official/workflow_templates/templates"

# Folded into every fingerprint: a change to how a stage is invoked must discard all state.
CONTROL_FILES = (
    "scripts/run_full_tests_windows.ps1",
    "scripts/run_full_tests_linux.sh",
    "scripts/gate_stages.py",
    "scripts/gate_backend.py",
    "scripts/gate_pytest_inventory.py",
)


@dataclass(frozen=True)
class Stage:
    """One gate stage, and the tracked paths whose content it depends on."""

    name: str
    inputs: tuple[str, ...] = field(default=())
    cacheable: bool = True

    def claims(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.inputs)


WHOLE_TREE = ("*",)

# Declarations are deliberate supersets.  Backend tests claim scripts/ because tests import from
# it; E2E claims the built sidebar because that is what the browser loads.
STAGES: tuple[Stage, ...] = (
    Stage("workspace link guard", inputs=(), cacheable=False),
    Stage("shipped artifact integrity", inputs=WHOLE_TREE, cacheable=False),
    Stage("pre-commit once (includes secret scan, lint, format, and typing)", inputs=WHOLE_TREE),
    Stage("package import", inputs=("comfyui_h3_context/*", "pyproject.toml")),
    Stage("frontend formatting", inputs=("frontend/*",)),
    Stage("frontend static contract", inputs=("frontend/*",)),
    Stage("frontend unit tests", inputs=("frontend/*", f"{TEMPLATE_CORPUS}/*")),
    Stage(
        "backend product tests",
        inputs=("comfyui_h3_context/*", "tests/*", "scripts/*", "pyproject.toml"),
        cacheable=False,
    ),
    Stage("security audit", inputs=WHOLE_TREE),
    Stage("frontend hermetic smoke", inputs=("frontend/*", "comfyui_h3_context/web/*")),
)

STAGE_NAMES = tuple(stage.name for stage in STAGES)

SMOKE_CASES = tuple(
    (f"journeys/{name}.spec.ts", title)
    for name, titles in {
        # M25-44: the compact editor's drag and playhead cases left with that surface; the
        # reference shell's four cases take their places.
        "nleReferenceShell": (
            "reference shell opens at viewport bounds with exactly four regions in proportion",
            "each splitter moves by pointer and keys and resets to its default",
            "export popover holds the final video card and returns focus",
            "clip editor tab has one editing surface and close returns focus to the launcher",
        ),
        "productionCore": (
            "page navigation, settings, reorder and release use one acknowledged body",
            "selection keeps a completed run authority and preview",
        ),
        "managedSequence": (
            "explicit start and exact terminal authority advance one serial child at a time",
            "detach revokes successor effects while the exact current child may terminalize",
        ),
        "nleWorkspace": (
            "smoke composition decodes real media and releases every owner on close",
            "explicit open, singleton, keyboard focus, resize and close/reopen",
            "canonical selection, trim and undo update the accepted geometry once",
        ),
        "nleProductionImport": (
            "pointer activation creates the first Authoring target, imports once, "
            "refreshes the catalog and highlights the row",
            "keyboard activation reuses the existing Authoring target and a second import "
            "of the same output is idempotent without touching the timeline",
            "Undo/Redo traverse the pre-import edit and the insertion "
            "while the imported library row persists",
        ),
        "authoringOutput": (
            "explicit native download and bounded preview lifecycle at responsive widths",
            "queued output requires explicit cancellation and exposes no artifact",
        ),
        "providerSettings": (
            "a remote census stays closed until credential and consent are both present",
            "withdrawing consent takes readiness back and is visible immediately",
            "the credential is never rendered, and the field empties as it is sent",
        ),
        "refusalReasons": ("typed anchor and generation refusals replace generic browser copy",),
    }.items()
    for title in titles
)
SMOKE_SPECS = tuple(dict.fromkeys(name for name, _ in SMOKE_CASES))


def browser_smoke(root: Path, *, list_only: bool = False) -> int:
    # IMPORTANT: keep exact file selection on the hermetic config. A package-script positional
    # filter can be swallowed and accidentally run the entire host or hardening lane.
    missing = [name for name in SMOKE_SPECS if not (root / "frontend/tests/e2e" / name).is_file()]
    if missing:
        print("Browser smoke spec missing: " + ", ".join(missing))
        return 2
    args = [
        PNPM,
        "--dir",
        "frontend",
        "exec",
        "playwright",
        "test",
        "--config",
        "playwright.config.ts",
        *SMOKE_SPECS,
        "--grep",
        r"(?:^|\s)(?:" + "|".join(re.escape(title) for _, title in SMOKE_CASES) + ")$",
    ]
    # IMPORTANT: whole files also contain five-minute performance cases. Check exact collected
    # cases so a rename, skip or broad selector cannot silently change the everyday smoke gate.
    collected = subprocess.run(
        [*args, "--list", "--reporter=json"],
        cwd=root,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if collected.returncode:
        return collected.returncode
    try:
        pending = json.loads(collected.stdout)["suites"]
        selected = []
        while pending:
            suite = pending.pop()
            pending.extend(suite.get("suites", []))
            for spec in suite.get("specs", []):
                for test in spec["tests"]:
                    if test["expectedStatus"] != "passed":
                        raise ValueError("Smoke case must be enabled and expected to pass")
                    selected.append((spec["file"].replace("\\", "/"), spec["title"]))
        if Counter(selected) != Counter(SMOKE_CASES):
            raise ValueError("Collected cases differ from SMOKE_CASES; review the selector")
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Browser smoke selection failed: {exc}")
        return 2
    print(f"Browser smoke: {len(selected)} cases in {len(SMOKE_SPECS)} files", flush=True)
    if list_only:
        for name, title in SMOKE_CASES:
            print(f"  {name}: {title}")
        return 0
    return subprocess.run(args, cwd=root, check=False).returncode


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run(args: list[str], root: Path) -> str:
    """Stdout on success, empty string on failure or a missing executable."""
    try:
        completed = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def tracked_files(root: Path) -> list[str]:
    """Every tracked path, POSIX-separated, sorted. Empty when git is unavailable."""
    return sorted(line for line in _run(["git", "ls-files"], root).splitlines() if line)


def _digest_paths(root: Path, paths: list[str]) -> str:
    """Content digest of a path set. Missing files hash as absent rather than raising."""
    accumulator = hashlib.sha256()
    for path in paths:
        accumulator.update(path.encode("utf-8"))
        accumulator.update(b"\0")
        try:
            accumulator.update(hashlib.sha256((root / path).read_bytes()).digest())
        except OSError:
            accumulator.update(b"<absent>")
        accumulator.update(b"\0")
    return accumulator.hexdigest()


def _tool_versions(root: Path) -> str:
    parts = [
        sys.version.split()[0],
        _run(["node", "-p", "process.versions.node"], root).strip(),
        _run([PNPM, "--version"], root).strip(),
    ]
    return "|".join(parts)


def narrow_stages() -> tuple[Stage, ...]:
    """Stages that declare something narrower than the whole tree.

    The catch-all is defined against these alone.  Measuring it against every stage would make it
    vacuous, because three stages already declare the whole tree and would claim every path.
    """
    return tuple(s for s in STAGES if s.inputs and s.inputs != WHOLE_TREE)


def unattributed_paths(paths: list[str]) -> list[str]:
    """Tracked paths no narrow stage declares.

    These are folded into *every* stage's fingerprint.  A file the backend suite happens to read
    but nobody declared -- anything under docs/ or examples/, say -- therefore invalidates the
    backend stage rather than silently invalidating nothing.  Under-declaration fails safe.
    """
    narrow = narrow_stages()
    return [p for p in paths if not any(stage.claims(p) for stage in narrow)]


def fingerprints(root: Path | None = None) -> dict[str, str]:
    """Current fingerprint per cacheable stage."""
    root = root or _repo_root()
    # IMPORTANT: frontend corpus tests read ignored templates. Their presence and bytes must
    # invalidate cached results too, or a changed/missing corpus silently keeps an old PASS.
    corpus_paths = [
        path.relative_to(root).as_posix()
        for path in (root / TEMPLATE_CORPUS).glob("*minimax_h3*.json")
    ]
    paths = sorted(set(tracked_files(root)) | set(corpus_paths))
    unattributed = _digest_paths(root, unattributed_paths(paths))
    control = _digest_paths(root, [p for p in CONTROL_FILES])
    versions = _tool_versions(root)

    result: dict[str, str] = {}
    for stage in STAGES:
        if not stage.cacheable:
            continue
        claimed = [p for p in paths if stage.claims(p)]
        accumulator = hashlib.sha256()
        accumulator.update(stage.name.encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(_digest_paths(root, claimed).encode("ascii"))
        accumulator.update(unattributed.encode("ascii"))
        accumulator.update(control.encode("ascii"))
        accumulator.update(versions.encode("utf-8"))
        result[stage.name] = accumulator.hexdigest()
    return result


def _state_path(root: Path) -> Path:
    return root / STATE_RELATIVE


def load_state(root: Path) -> dict[str, str]:
    """Recorded stage -> fingerprint. A corrupt or partial file is treated as empty."""
    try:
        raw = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return {}
    passed = raw.get("passed")
    if not isinstance(passed, dict):
        return {}
    return {str(k): str(v) for k, v in passed.items() if isinstance(v, str)}


def record_pass(name: str, root: Path | None = None) -> None:
    root = root or _repo_root()
    current = fingerprints(root)
    if name not in current:
        return
    state = load_state(root)
    state[name] = current[name]
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": STATE_VERSION, "passed": state}, indent=1, sort_keys=True),
        encoding="utf-8",
    )


def should_run(name: str, resume: bool, root: Path | None = None) -> bool:
    """True when the stage must execute. Without resume this is unconditionally True."""
    if not resume:
        return True
    root = root or _repo_root()
    stage = next((s for s in STAGES if s.name == name), None)
    if stage is None or not stage.cacheable:
        return True
    return load_state(root).get(name) != fingerprints(root).get(name)


def _cmd_should_run(args: argparse.Namespace) -> int:
    # Exit 0 means run, 1 means skip: the runners branch on the exit code.
    return 0 if should_run(args.name, args.resume) else 1


def _cmd_record(args: argparse.Namespace) -> int:
    record_pass(args.name)
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    root = _repo_root()
    state = load_state(root)
    current = fingerprints(root)
    skipped = [n for n in STAGE_NAMES if n in current and state.get(n) == current[n]]
    if not args.resume:
        print("resume not requested: every stage ran, no cache was read or written")
        return 0
    if not skipped:
        print("resume requested: no stage was eligible to skip")
        return 0
    print("resume requested; valid stage records (see RUN/SKIP output for this execution):")
    for name in skipped:
        print(f"  {name}  {current[name][:16]}")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    _state_path(_repo_root()).unlink(missing_ok=True)
    (_repo_root() / ".tmp/backend-gate/state.json").unlink(missing_ok=True)
    print("gate state discarded")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    run = sub.add_parser("should-run")
    run.add_argument("name")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=_cmd_should_run)

    rec = sub.add_parser("record")
    rec.add_argument("name")
    rec.set_defaults(func=_cmd_record)

    summary = sub.add_parser("summary")
    summary.add_argument("--resume", action="store_true")
    summary.set_defaults(func=_cmd_summary)

    reset = sub.add_parser("reset")
    reset.set_defaults(func=_cmd_reset)

    smoke = sub.add_parser("browser-smoke")
    smoke.add_argument("--list", action="store_true")
    smoke.set_defaults(func=lambda args: browser_smoke(_repo_root(), list_only=args.list))

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
