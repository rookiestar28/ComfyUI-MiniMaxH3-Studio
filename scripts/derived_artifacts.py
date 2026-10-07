"""The repository's derived artifacts, declared once and ordered from the declaration.

Nineteen scripts under `scripts/` regenerate something, and the order they must run in is real,
load-bearing and, until now, written down only as prose in `tests/TEST_SOP.md` section 3.3.4. Two
items got it wrong in one day despite that section existing: M23-46 regenerated the contract
inventory after the first provenance write, so the bundle embedded a `resolved_dependencies_sha256`
that was already stale, and M23-47 hand-edited a contract JSON that `frontend/src` imports without
realising it was a bundle input. Both produced a tree that looked self-consistent -- every generator
`--check` passed -- and both were caught only by the offline rebuild parity inside the backend stage
of the Full Gate, hours later. A person cannot hold this graph, and prose cannot enforce it.

Everything here was measured rather than read, because reading is what fails:

- **Outputs** were established by deleting each generator's predicted outputs in a tree seeded from
  the tracked files, running `--write`, and diffing the whole tree. No generator wrote outside its
  prediction. Static path constants would have over-reported badly -- `architecture_fitness` names
  ten contracts and writes one -- and under-reported the five generators that build their output
  path from a directory constant.
- **Edges** were established with a `sys.addaudithook` `open` hook recording every tracked file each
  generator actually reads. That is what separates naming from reading: `contract_inventory` names
  `fingerprint_domain_v1.json` and never opens it, so the cycle a text scan reports there is not
  real, while the cycle recorded in `CYCLE` below is.
"""

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
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Where a generator can run. `relocatable` needs only the tracked bytes and can therefore be run in
#: a temporary tree and diffed, which is the only honest way to verify a candidate before it touches
#: a final path. `in_repository` consults git history and cannot: giving a temporary work tree a
#: `.git` that points at the real object store is exactly the arrangement AGENTS.md forbids around
#: worktrees and links, so those are computed against real history instead.
Locus = Literal["relocatable", "in_repository"]

#: The two roots a generator can write a contract to. `CONTRACTS` is packaged and installed;
#: `GOVERNANCE` sits outside `comfyui_h3_context*` and is excluded from the wheel, the sdist
#: and the Registry archive. M23-55 moved every artifact with no runtime or bundle reader to
#: the second one, so which prefix an output carries is a statement about who reads it.
CONTRACTS = "comfyui_h3_context/contracts/"
GOVERNANCE = "governance/contracts/"


@dataclass(frozen=True, slots=True)
class Generator:
    """One regenerating script, its measured outputs, and what it needs in order to run."""

    name: str
    outputs: tuple[str, ...]
    reads: tuple[str, ...] = ()
    locus: Locus = "relocatable"
    check: tuple[str, ...] = ("--check",)
    #: True when the generator opens one of its own outputs. Such a file must never be unlinked
    #: before its replacement is in place -- see `core_public_surface`, whose output is the package
    #: `__init__` the generator itself has to import.
    self_reading: bool = False
    #: Tools that must exist outside this repository for `--write` to succeed.
    external_tools: tuple[str, ...] = ()
    #: True when the generator's inputs include the repository's own position, not only files.
    reads_head: bool = False
    notes: str = ""


_SCANS_EVERYTHING = (
    "architecture_fitness",
    "architecture_inventory",
    "authoring_output_schemas",
    "authoring_render_schemas",
    "build_provenance",
    "canonical_cross_language_vectors",
    "closeout_matrix",
    "contract_inventory",
    "core_public_surface",
    "cross_language_surface",
    "duration_resolution_contract",
    "fingerprint_domain",
    "localized_identity_registry",
    "m16_01_source_requalification",
    "m16_02_reconstruction_regression",
    "m19_closeout",
    "m25_16_scene_parity",
    "managed_run_stategraph",
    "nle_hardening_manifest",
    "nle_semantic_conformance_manifest",
    "node_surface",
    "packaged_artifact_readership",
    "public_surface",
    "supply_chain_manifest",
)


GENERATORS: tuple[Generator, ...] = (
    Generator(
        name="authoring_output_schemas",
        outputs=(GOVERNANCE + "authoring_output_v1.schema.json",),
        reads=("core_public_surface",),
        notes="Closed tooling-only output transport schema derived from the pure protocol.",
    ),
    Generator(
        name="authoring_render_schemas",
        outputs=(
            GOVERNANCE + "authoring_render_job_request_v1.schema.json",
            GOVERNANCE + "authoring_render_receipt_v1.schema.json",
        ),
        notes="Closed tooling-only request and receipt schemas derived from pure dataclass fields.",
        reads=("core_public_surface",),
    ),
    Generator(
        name="core_public_surface",
        outputs=("comfyui_h3_context/core/__init__.py",),
        self_reading=True,
        notes=(
            "Writes shipped runtime source, not a contract. Its output is also its input: with the "
            "file deleted the generator fails before producing anything, so an apply must replace "
            "it by rename and must never unlink it first."
        ),
    ),
    Generator(
        name="localized_identity_registry",
        outputs=("frontend/src/i18n/generatedBackendIdentities.ts",),
        notes="Writes into `frontend/src`, so it is a bundle input by construction.",
    ),
    Generator(
        name="duration_resolution_contract",
        outputs=("frontend/src/contracts/generatedDurationResolution.ts",),
        notes=(
            "Executes the pure Python length authority for the closed product duration domain and "
            "writes the typed browser tuple table into `frontend/src`."
        ),
    ),
    Generator(
        name="cross_language_surface",
        outputs=(
            CONTRACTS + "cross_language_surface_v1.json",
            "frontend/src/contracts/generatedSurface.ts",
        ),
        reads=("core_public_surface", "duration_resolution_contract"),
        notes=(
            "Owns two outputs that must move together, one of them under `frontend/src`. A "
            "declaration keyed by output path would let half a pair be selected."
        ),
    ),
    Generator(
        name="public_surface",
        outputs=(GOVERNANCE + "public_surface_v1.json",),
        reads=("core_public_surface",),
    ),
    Generator(
        name="packaged_artifact_readership",
        outputs=(GOVERNANCE + "packaged_artifact_readership_v1.json",),
        notes=(
            "Classifies every packaged JSON by who opens it. It declares no read edge because it "
            "asks `git grep` rather than opening the artifacts, so its dependency is on which "
            "files exist rather than on what any of them contains -- but its own output is a "
            "packaged JSON, so the two generators that scan the directory read it."
        ),
    ),
    Generator(
        name="node_surface",
        outputs=(GOVERNANCE + "node_surface_v1.json",),
    ),
    Generator(
        name="managed_run_stategraph",
        outputs=(GOVERNANCE + "managed_run_stategraph_v1.json",),
    ),
    Generator(
        name="canonical_cross_language_vectors",
        outputs=("tests/fixtures/canonical_cross_language_vectors.json",),
    ),
    Generator(
        name="m25_16_scene_parity",
        outputs=("tests/fixtures/m25_16_scene_resolver_parity_v1.json",),
        reads=("core_public_surface",),
        notes=(
            "Shared scene-resolver parity vectors derived from the pure Python "
            "`resolve_composition` over the accepted M25-10 composition fixture; the frontend "
            "resolver port is checked against the same file."
        ),
    ),
    Generator(
        name="nle_hardening_manifest",
        outputs=(GOVERNANCE + "nle_hardening_coverage_manifest_v1.json",),
        reads=(),
        notes=(
            "M25-21 hardening coverage membership: the 34 canonical commands, the UI and action "
            "rows, the recovery seams and the frozen measurements, each bound to the evidence id "
            "of the test that observes it. It reads nothing generated -- the membership is "
            "authored in the generator itself -- so it is ordered by name rather than by an input."
        ),
    ),
    Generator(
        name="nle_semantic_conformance_manifest",
        outputs=(GOVERNANCE + "nle_semantic_conformance_manifest_v1.json",),
        reads=("core_public_surface",),
        notes=(
            "M25-20 conformance membership: the closed corpus joined to the authored M25-16 "
            "control-coverage rows. It also reads two authored files no generator owns -- the "
            "control-coverage manifest and the sidebar shell contract -- and records their "
            "digests, so editing either one drifts this output rather than leaving the join stale."
        ),
    ),
    Generator(
        name="m16_01_source_requalification",
        outputs=("tests/fixtures/m16_01_source_requalification.json",),
        self_reading=True,
        notes="Its output path is `--ledger`; the declared path is that flag's default.",
    ),
    Generator(
        name="m16_02_reconstruction_regression",
        outputs=("tests/fixtures/m16_02_reconstruction_regression.json",),
        self_reading=True,
        notes="Its output path is `--matrix`; the declared path is that flag's default.",
    ),
    Generator(
        name="m19_closeout",
        outputs=(GOVERNANCE + "m19_closeout_v1.json",),
        locus="in_repository",
        self_reading=True,
        notes="Resolves historical commits with `git rev-parse`.",
    ),
    Generator(
        name="closeout_matrix",
        outputs=(GOVERNANCE + "closeout_matrix_v1.json",),
        locus="in_repository",
        self_reading=True,
        notes="Reads historical file contents with `git show <commit>:<path>`.",
    ),
    Generator(
        name="architecture_inventory",
        outputs=(GOVERNANCE + "architecture_inventory_v1.json",),
        reads=("core_public_surface",),
    ),
    Generator(
        name="architecture_fitness",
        outputs=(GOVERNANCE + "architecture_fitness_v1.json",),
        reads=(
            "core_public_surface",
            "cross_language_surface",
            "duration_resolution_contract",
            "localized_identity_registry",
        ),
        notes=(
            "Names ten contracts in its ownership rules and writes one. It also goes stale "
            "whenever any Python or TypeScript file is added or removed, test files included."
        ),
    ),
    Generator(
        name="contract_inventory",
        outputs=(CONTRACTS + "contract_inventory_v1.json",),
        reads=(
            "architecture_inventory",
            "authoring_output_schemas",
            "authoring_render_schemas",
            "build_provenance",
            "canonical_cross_language_vectors",
            "closeout_matrix",
            "core_public_surface",
            "cross_language_surface",
            "duration_resolution_contract",
            "localized_identity_registry",
            "m16_01_source_requalification",
            "m16_02_reconstruction_regression",
            "m19_closeout",
            "m25_16_scene_parity",
            "managed_run_stategraph",
            "node_surface",
            "packaged_artifact_readership",
            "public_surface",
            "supply_chain_manifest",
        ),
        notes="Scans the tree, so it reads almost every other generator's output.",
    ),
    Generator(
        name="build_provenance",
        outputs=(CONTRACTS + "build_provenance_v1.json",),
        reads=(
            "contract_inventory",
            "cross_language_surface",
            "duration_resolution_contract",
            "localized_identity_registry",
            "public_surface",
        ),
        locus="in_repository",
        self_reading=True,
        reads_head=True,
        notes=(
            "Derives `source_commit` and `source_tree` from `git rev-parse HEAD` unless "
            "`--reuse-source-revision` is given, and the bundle compiles that identity in. So "
            "the repository's own position is an input: regenerating after a commit moves the "
            "bundle digest with no file input having changed."
        ),
    ),
    Generator(
        name="supply_chain_manifest",
        outputs=(GOVERNANCE + "sbom.spdx.json", GOVERNANCE + "supply_chain_v1.json"),
        reads=(
            "build_provenance",
            "cross_language_surface",
            "duration_resolution_contract",
            "localized_identity_registry",
        ),
        check=("--emit", "validate"),
        self_reading=True,
        external_tools=("pnpm",),
        notes=(
            "Owns two outputs. It has no `--check`: it validates through `--emit validate`, so a "
            "declaration that assumes the `--check` convention breaks on the last generator in the "
            "order, which is where it would be hardest to read."
        ),
    ),
    Generator(
        name="fingerprint_domain",
        outputs=(GOVERNANCE + "fingerprint_domain_v1.json",),
        reads=("contract_inventory",),
    ),
    Generator(
        name="retirement_eligibility",
        outputs=(GOVERNANCE + "retirement_eligibility_v1.json",),
        reads=_SCANS_EVERYTHING,
        notes="Reads every other artifact, so it is last in any order that respects the edges.",
    ),
)

#: The one place the measured graph is not a DAG. `contract_inventory` indexes the provenance file,
#: the provenance embeds the inventory's digest, and the supply-chain manifest hashes the provenance
#: while the inventory indexes the manifest. No topological sort exists over these three.
#:
#: CRITICAL: `CYCLE_SEQUENCE` is not a modelling convenience, it is `tests/TEST_SOP.md` 3.3.4, and
#: the reason it converges is that `source_inputs_sha256` hashes `frontend/src` and the fixed build
#: authorities but never the emitted bundle -- so the second provenance write is a fixed point
#: rather than the start of a loop. Reordering these steps, or dropping the second provenance write,
#: reintroduces the M23-46 defect exactly: a bundle carrying a dependency digest that is already
#: stale, which nothing local reports and only the gate's offline rebuild parity sees.
CYCLE: tuple[str, ...] = ("build_provenance", "contract_inventory", "supply_chain_manifest")

#: The bundle build is not a generator -- it has no `--write` and this module never runs it -- but
#: it sits inside the cycle's sequence and the order means nothing without it.
BUNDLE_BUILD = "pnpm --dir frontend run build"

CYCLE_SEQUENCE: tuple[str, ...] = (
    "contract_inventory",
    "build_provenance",
    BUNDLE_BUILD,
    "build_provenance",
    "supply_chain_manifest",
)

_CYCLE_NODE = "\x00cycle"


class DeclarationError(RuntimeError):
    """The declaration disagrees with itself."""


def by_name() -> dict[str, Generator]:
    return {generator.name: generator for generator in GENERATORS}


def validate() -> None:
    """Refuse a declaration that is internally inconsistent, before anything reads it."""

    known = by_name()
    if len(known) != len(GENERATORS):
        raise DeclarationError("two generators share a name")
    owners: dict[str, str] = {}
    for generator in GENERATORS:
        if not generator.outputs:
            raise DeclarationError(f"{generator.name} declares no output")
        for path in generator.outputs:
            if path in owners:
                raise DeclarationError(f"{path} is claimed by {owners[path]} and {generator.name}")
            owners[path] = generator.name
        for read in generator.reads:
            if read not in known:
                raise DeclarationError(f"{generator.name} reads unknown generator {read}")
            if read == generator.name:
                raise DeclarationError(f"{generator.name} declares itself as an input")
    for name in CYCLE:
        if name not in known:
            raise DeclarationError(f"the cycle names unknown generator {name}")
    sequenced = {step for step in CYCLE_SEQUENCE if step != BUNDLE_BUILD}
    if sequenced != set(CYCLE):
        raise DeclarationError("the cycle sequence does not cover the cycle exactly")


def order() -> tuple[str, ...]:
    """The run order, derived from the declared edges with the one cycle collapsed.

    The three members of `CYCLE` are treated as a single node whose internal order is
    `CYCLE_SEQUENCE`; every other edge is honoured by a deterministic topological sort.
    """

    validate()
    cycle = set(CYCLE)

    def collapse(name: str) -> str:
        return _CYCLE_NODE if name in cycle else name

    incoming: dict[str, set[str]] = {}
    for generator in GENERATORS:
        target = collapse(generator.name)
        incoming.setdefault(target, set())
        for read in generator.reads:
            source = collapse(read)
            if source != target:
                incoming[target].add(source)

    ready = sorted(name for name, sources in incoming.items() if not sources)
    resolved: list[str] = []
    while ready:
        current = ready.pop(0)
        resolved.append(current)
        freed = [
            name
            for name, sources in incoming.items()
            if current in sources
            and not (sources - {current})
            and name not in resolved
            and name not in ready
        ]
        for sources in incoming.values():
            sources.discard(current)
        ready = sorted(ready + freed)
    if len(resolved) != len(incoming):
        stuck = sorted(set(incoming) - set(resolved))
        raise DeclarationError(f"the declared edges do not sort: {stuck}")

    steps: list[str] = []
    for name in resolved:
        if name == _CYCLE_NODE:
            steps.extend(CYCLE_SEQUENCE)
        else:
            steps.append(name)
    return tuple(steps)


def plan() -> Iterator[tuple[int, str, Generator | None]]:
    """The order, paired with each step's declaration; the bundle build has none."""

    known = by_name()
    for position, step in enumerate(order(), start=1):
        yield position, step, None if step == BUNDLE_BUILD else known[step]


def describe() -> dict[str, object]:
    known = by_name()
    return {
        "schema": "h3-context-derived-artifacts/1",
        "generator_count": len(GENERATORS),
        "output_count": sum(len(generator.outputs) for generator in GENERATORS),
        "cycle": list(CYCLE),
        "cycle_sequence": list(CYCLE_SEQUENCE),
        "order": list(order()),
        "in_repository": sorted(g.name for g in GENERATORS if g.locus == "in_repository"),
        "notes": {g.name: g.notes for g in GENERATORS if g.notes},
        "self_reading": sorted(g.name for g in GENERATORS if g.self_reading),
        "reads_head": sorted(g.name for g in GENERATORS if g.reads_head),
        "external_tools": {g.name: list(g.external_tools) for g in GENERATORS if g.external_tools},
        "outputs": {name: list(known[name].outputs) for name in sorted(known)},
    }


ROOT = Path(__file__).resolve().parents[1]

#: Where a dry-run builds its candidate tree. Ignored, and removed on both the success and the
#: failure path.
WORKSPACE = ROOT / ".tmp" / "derived-artifacts"

#: Windows marks a directory junction with this attribute. `scripts/tooling_retirement.py` uses the
#: same constant for the same reason: `Path.is_symlink()` does not report a junction.
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class TransactionError(RuntimeError):
    """The transaction refused to proceed, and nothing was written."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One declared output, as the dry-run computed it."""

    path: str
    generator: str
    verdict: Literal["unchanged", "changed", "created"]
    before_sha256: str | None
    after_sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class Transaction:
    """Everything a dry-run learned, with nothing written to a final path."""

    head: str
    candidates: tuple[Candidate, ...]
    failures: tuple[tuple[str, str], ...] = ()
    bundle_rebuild_reasons: tuple[str, ...] = ()

    @property
    def changed(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.verdict != "unchanged")

    @property
    def ok(self) -> bool:
        return not self.failures


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def git(*arguments: str, cwd: Path | None = None) -> str:
    result = _run(["git", *arguments], cwd=cwd or ROOT)
    if result.returncode != 0:
        raise TransactionError(f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def working_tree_files() -> tuple[str, ...]:
    """Every file the next commit would carry: tracked, plus untracked and not ignored.

    CRITICAL: `git ls-files` alone is the wrong set. A transaction is about the tree that is about
    to be committed, and a new file that is staged-to-be is exactly the kind of input that moves
    `contract_inventory` -- so seeding from tracked files only would report "unchanged" for an
    artifact that is genuinely stale, which is the one answer a dry-run must never give.
    """

    tracked = git("ls-files").splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    return tuple(sorted({line for line in (*tracked, *untracked) if line}))


def bundle_input_contracts() -> tuple[str, ...]:
    """Contract JSONs the bundle compiles in, found by import rather than by path convention.

    CRITICAL: this is computed, never listed. `host_seam_census_v1.json` reads like a governance
    record -- hand-maintained, no `--write` -- and `frontend/src/host/hostSeamContract.ts` imports
    it, so editing it changes the shipped bundle. Every heuristic short of reading the imports puts
    it on the wrong side, which is precisely how M23-47 spent a gate run.
    """

    pattern = re.compile(r"contracts/([A-Za-z0-9_.-]+\.json)")
    found: set[str] = set()
    source = ROOT / "frontend" / "src"
    for path in source.rglob("*"):
        if path.suffix not in {".ts", ".tsx"} or not path.is_file():
            continue
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            candidate = CONTRACTS + name
            if (ROOT / candidate).is_file():
                found.add(candidate)
    return tuple(sorted(found))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_link(path: Path) -> bool:
    """Whether a path is a symlink or a Windows directory junction.

    GUARD: `Path.is_symlink()` returns **False** for a junction, so it is not a sufficient test on
    Windows and the reparse attribute has to be read directly. AGENTS.md section 3 records what
    happens when a tool misses this: `git worktree remove` follows a junction and deletes the link
    target's contents with no diagnostic. `scripts/tooling_retirement.py::_is_reparse` is the same
    check for the same reason.
    """

    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
    except OSError:
        return True  # unreadable: treat as a link rather than walk into it
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE)


def _real_files_under(path: Path) -> Iterator[Path]:
    """Every real file under `path`, never descending through a link.

    `Path.rglob` walks into a junction, because a junction answers `is_dir()` truthfully and
    `is_symlink()` falsely, so globbing to clear read-only bits would chmod files outside the tree.
    """

    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            candidate = Path(entry.path)
            if _is_link(candidate):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(candidate)
            elif entry.is_file(follow_symlinks=False):
                yield candidate


def _remove_tree(path: Path, *, tolerant: bool = False) -> None:
    """Remove a scratch tree, read-only git objects included.

    GUARD: `git clone` marks loose objects and pack files read-only, and on Windows
    `shutil.rmtree` cannot unlink a read-only file -- it raises `PermissionError: [WinError 5]`.
    `ignore_errors=True` does not make that safe, it makes it invisible: the workspace survives,
    and the *next* run fails inside `_seed` on a directory it believed it had removed. Clear the
    bits first, and let `_seed` fail loudly if the removal still does not succeed.

    The bits are cleared by walking rather than by `rmtree`'s error callback because that callback
    is spelled `onerror` before Python 3.12 and `onexc` from 3.12, and this package supports 3.10.
    The walk never descends through a link, and a link at `path` itself is refused outright: `chmod`
    and `rmtree` both follow a resolved junction, and AGENTS.md is explicit that a link escaping a
    scratch tree must never be followed by anything that then deletes.
    """

    if not path.exists():
        return
    # CRITICAL: refuse a link BEFORE resolving, and keep these two lines in this order. `resolve()`
    # is needed because `parents` compares lexically, so `.tmp/../comfyui_h3_context` would satisfy
    # "inside the scratch directory" while naming a path that is not -- but resolving a junction
    # hands `shutil.rmtree` the link's target, and `rmtree` refuses a junction while deleting a
    # real directory happily. Resolving first therefore converts a safe refusal into a silent
    # delete of whatever the link points at. `.tmp/` is where this repository keeps its worktrees,
    # and AGENTS.md section 3 records this exact failure for `git worktree remove`.
    if _is_link(path):
        raise TransactionError(f"refusing to remove {path}: it is a link, not a directory")
    path = path.resolve()
    # GUARD: a whitelist, not a blacklist. "Not the repository root and not its parent" still
    # permits `ROOT/comfyui_h3_context`, and the caller of a removal helper is exactly where a
    # later refactor gets a path wrong. A removable path is either inside the scratch directory or
    # entirely outside the repository; everything else is refused, so the failure mode of a bad
    # argument is an exception rather than an unrecoverable delete.
    scratch = (ROOT / ".tmp").resolve()
    inside_scratch = path == scratch or scratch in path.parents
    overlaps_repository = path == ROOT or ROOT in path.parents or path in ROOT.parents
    if overlaps_repository and not inside_scratch:
        raise TransactionError(
            f"refusing to remove {path}: it is inside the repository but outside {scratch}"
        )
    for child in _real_files_under(path):
        try:
            child.chmod(stat.S_IWRITE)
        except OSError:
            pass
    try:
        shutil.rmtree(path)
    except OSError:
        if not tolerant:
            raise


def _seed(destination: Path) -> None:
    """Build a tree the generators can run in: real history, plus the working tree's tracked bytes.

    CRITICAL: the history comes from `git clone --local`, which makes an independent repository.
    Pointing a temporary work tree at the real `.git` instead -- through a `gitdir:` file or
    `GIT_DIR` -- would let anything running in the copy write to the real object store, and is the
    same class of arrangement AGENTS.md forbids around worktrees and links. The clone is also what
    makes `build_provenance`, `closeout_matrix` and `m19_closeout` runnable here at all: without
    history they fail before producing anything.
    """

    _remove_tree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # GUARD: `--no-checkout`. Cloning with a checkout materializes `HEAD`, and an overlay can only
    # add or replace -- it can never take a file away, so a path the candidate DELETES survives in
    # the workspace and `git add --all` re-tracks it. Every generator then sees a file the tree
    # under test does not have: `contract_inventory` indexes a removed contract, the readership
    # scan finds a removed artifact, and an `apply` writes that answer over correct final bytes.
    # An empty checkout plus an explicit copy of exactly `working_tree_files()` makes the workspace
    # the candidate by construction rather than by correction.
    result = _run(
        [
            "git",
            "clone",
            "--local",
            "--no-hardlinks",
            "--no-checkout",
            "--quiet",
            str(ROOT),
            str(destination),
        ],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise TransactionError("could not clone the repository into the workspace")
    # Every file the next commit would carry, and nothing else: an uncommitted edit and a new file
    # not yet added are exactly the cases a dry-run exists for.
    for name in working_tree_files():
        source = ROOT / name
        if not source.is_file():
            # A tracked path missing from the working tree is an unstaged deletion. Which tree the
            # transaction is about is genuinely ambiguous there -- `git commit` would keep the file
            # and `git commit -a` would drop it -- so say so instead of guessing.
            raise TransactionError(
                f"{name} is tracked but absent from the working tree; "
                "stage the deletion before running a transaction"
            )
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # GUARD: stage the workspace. Some generators ask git which files exist rather than walking the
    # filesystem -- `packaged_artifact_readership` runs `git grep`, which searches tracked files
    # only. Without this the index still describes `HEAD`: additions are invisible, so the dry-run
    # reports a fresh artifact as changed, and deletions are not recorded at all. `--all` stages
    # both directions, which is why it must follow the copy rather than precede it.
    staged = _run(["git", "add", "--all"], cwd=destination)
    if staged.returncode != 0:
        raise TransactionError("could not stage the workspace overlay")


def dry_run(*, workspace: Path | None = None, keep: bool = False) -> Transaction:
    """Compute every declared output in a seeded tree. No final path is opened for writing."""

    validate()
    workspace = workspace or WORKSPACE
    head = git("rev-parse", "HEAD")
    bundle_inputs = set(bundle_input_contracts())
    known = by_name()
    candidates: list[Candidate] = []
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        _seed(workspace)
        for step in order():
            if step == BUNDLE_BUILD:
                # Deliberately not executed: the bundle build needs `frontend/node_modules`, which
                # is untracked and must never be linked into a temporary tree. The transaction
                # reports that a rebuild is implied instead of pretending to have done one.
                continue
            generator = known[step]
            result = _run(
                [sys.executable, f"scripts/{generator.name}.py", "--write"], cwd=workspace
            )
            if result.returncode != 0:
                detail = result.stdout.strip() or result.stderr.strip() or "no output"
                failures.append((generator.name, detail.splitlines()[0][:200]))
                continue
            if generator.check:
                verified = _run(
                    [sys.executable, f"scripts/{generator.name}.py", *generator.check],
                    cwd=workspace,
                )
                if verified.returncode != 0:
                    detail = verified.stdout.strip() or verified.stderr.strip() or "no output"
                    first = detail.splitlines()[0][:160]
                    failures.append((generator.name, f"check failed after write: {first}"))
                    continue
            for path in generator.outputs:
                # A generator that appears twice in the order -- `build_provenance` does,
                # around the bundle build -- is recorded from its FIRST run. That is correct only
                # while the bundle build is skipped here, which keeps the two runs byte-identical.
                # Executing the build in the workspace would make the second run the truthful one.
                if path in seen:
                    continue
                seen.add(path)
                produced = workspace / path
                if not produced.is_file():
                    failures.append((generator.name, f"declared output {path} was not produced"))
                    continue
                after = produced.read_bytes()
                final = ROOT / path
                before = final.read_bytes() if final.is_file() else None
                if before is None:
                    verdict: Literal["unchanged", "changed", "created"] = "created"
                elif before == after:
                    verdict = "unchanged"
                else:
                    verdict = "changed"
                candidates.append(
                    Candidate(
                        path=path,
                        generator=generator.name,
                        verdict=verdict,
                        before_sha256=_digest(before) if before is not None else None,
                        after_sha256=_digest(after),
                        size=len(after),
                    )
                )
    finally:
        if not keep:
            _remove_tree(workspace, tolerant=True)

    return Transaction(
        head=head,
        candidates=tuple(candidates),
        failures=tuple(failures),
        bundle_rebuild_reasons=bundle_rebuild_reasons(
            candidates,
            bundle_inputs,
            head=head,
            recorded_head=_recorded_head(),
            differs_from_head=_differs_from_head,
        ),
    )


def bundle_rebuild_reasons(
    candidates: Sequence[Candidate],
    bundle_inputs: Collection[str],
    *,
    head: str,
    recorded_head: str | None,
    differs_from_head: Callable[[str], bool],
) -> tuple[str, ...]:
    """Every reason the shipped bundle would move, given a computed candidate set.

    Pure, and separated from `dry_run` on purpose: the four detection paths below are the whole
    value of this module, and observing them through `dry_run` costs a clone per assertion, which
    is how they ended up asserted by reading the source instead of by running it.
    """

    reasons: list[str] = []
    for candidate in candidates:
        if candidate.verdict == "unchanged":
            continue
        if candidate.path.startswith("frontend/src/") or candidate.path in bundle_inputs:
            reasons.append(f"{candidate.path} is a bundle input and changed")
        elif candidate.generator == "build_provenance":
            # CRITICAL: the provenance is not imported by any `frontend/src` module, so neither the
            # import scan nor the path prefix sees it -- and it is compiled in all the same, through
            # `frontend/buildProvenance.ts`. Its `resolved_dependencies_sha256` covers the contract
            # inventory, so a change anywhere upstream of that inventory reaches the bundle by this
            # route and no other. Dropping this branch is how M23-46's defect returns.
            reasons.append(
                f"{candidate.path} changed, and the bundle compiles its embedded identity in"
            )
    # CRITICAL: a bundle input that no generator owns is invisible to the loop above, because the
    # loop only ever sees declared outputs. Three of the four contracts `frontend/src` imports --
    # the host-seam census, its shape fixture and the official asset table -- are hand-maintained
    # and have no `--write` at all, so a hand edit to one of them changes the shipped bundle and
    # produces no candidate. That is M23-47 exactly, and it is the case this whole module exists to
    # move earlier than the gate. Iterate the inputs the candidates do NOT own; iterating the
    # candidates, or the owned set, restores the blind spot while looking almost identical.
    owned = {candidate.path for candidate in candidates}
    for path_name in sorted(set(bundle_inputs) - owned):
        if differs_from_head(path_name):
            reasons.append(
                f"{path_name} is a bundle input, is owned by no generator, "
                "and differs from its committed bytes"
            )
    if recorded_head is not None and recorded_head != head:
        reasons.append(
            f"build_provenance records {recorded_head[:12]} and HEAD is {head[:12]}, "
            "so the bundle's embedded identity would move"
        )
    return tuple(dict.fromkeys(reasons))


def _differs_from_head(path_name: str) -> bool:
    """Whether the working tree's copy of a path differs from the bytes `HEAD` carries.

    Used for inputs no generator owns, where there is nothing to regenerate and therefore nothing
    to diff a candidate against. A path absent from `HEAD` is a new file and counts as differing.
    """

    final = ROOT / path_name
    if not final.is_file():
        return False
    committed = subprocess.run(["git", "show", f"HEAD:{path_name}"], cwd=ROOT, capture_output=True)
    if committed.returncode != 0:
        return True  # absent from HEAD: a new file, which is a change by any reading
    return committed.stdout != final.read_bytes()


def _recorded_head() -> str | None:
    """The commit `build_provenance_v1.json` currently names, or None when it cannot be read."""

    path = ROOT / CONTRACTS / "build_provenance_v1.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        recorded = payload["external_parameters"]["source_commit"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return recorded if isinstance(recorded, str) else None


def apply_transaction(
    workspace: Path, transaction: Transaction, *, root: Path | None = None
) -> tuple[str, ...]:
    """Move every changed candidate onto its final path, or move none of them.

    Everything is read and verified before anything is written: a candidate whose bytes no longer
    match what the dry-run measured aborts the whole transaction with no final path touched.

    CRITICAL: each replacement is a rename over the existing file, never an unlink followed by a
    write. `core_public_surface` writes `comfyui_h3_context/core/__init__.py`, which it also has to
    import; a window in which that path does not exist leaves the package unimportable, and a
    failure inside that window would leave the repository broken rather than unchanged.
    """

    if not transaction.ok:
        raise TransactionError(
            "the dry-run reported failures, so nothing is applied: "
            + "; ".join(f"{name}: {detail}" for name, detail in transaction.failures)
        )
    destination = root or ROOT
    staged: list[tuple[Path, bytes]] = []
    for candidate in transaction.changed:
        produced = workspace / candidate.path
        if not produced.is_file():
            raise TransactionError(f"{candidate.path} is missing from the workspace")
        data = produced.read_bytes()
        if _digest(data) != candidate.after_sha256:
            raise TransactionError(f"{candidate.path} changed under the transaction")
        staged.append((destination / candidate.path, data))
    written: list[str] = []
    for final, data in staged:
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(final.name + ".derived-artifacts.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, final)
        written.append(final.relative_to(destination).as_posix())
    return tuple(written)


def receipt(transaction: Transaction, written: tuple[str, ...]) -> dict[str, object]:
    """What the transaction did, in fingerprints and repo-relative paths only."""

    return {
        "schema": "h3-context-derived-artifact-transaction/1",
        "observed_head": transaction.head,
        "generator_versions": {
            generator.name: _digest((ROOT / "scripts" / f"{generator.name}.py").read_bytes())
            for generator in GENERATORS
        },
        "outputs": [
            {
                "path": candidate.path,
                "generator": candidate.generator,
                "verdict": candidate.verdict,
                "before_sha256": candidate.before_sha256,
                "after_sha256": candidate.after_sha256,
                "size": candidate.size,
            }
            for candidate in transaction.candidates
        ],
        "written": list(written),
        "bundle_rebuild_reasons": list(transaction.bundle_rebuild_reasons),
        "failures": [
            {"generator": name, "detail": detail} for name, detail in transaction.failures
        ],
    }


def _report(transaction: Transaction, written: tuple[str, ...]) -> None:
    for candidate in transaction.candidates:
        if candidate.verdict == "unchanged":
            continue
        print(f"{candidate.verdict:>9}  {candidate.path}  ({candidate.generator})")
    if not transaction.changed:
        print("every declared output is already at its fixed point")
    for reason in transaction.bundle_rebuild_reasons:
        print(f"  bundle rebuild implied: {reason}")
    for name, detail in transaction.failures:
        print(f"    FAILED  {name}: {detail}")
    for path in written:
        print(f"  written  {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Declared derived artifacts and their run order.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="print the derived run order")
    commands.add_parser("describe", help="print the whole declaration as JSON")
    commands.add_parser("bundle-inputs", help="print the contract JSONs the bundle compiles in")
    dry = commands.add_parser("dry-run", help="compute every output without writing one")
    dry.add_argument("--receipt", action="store_true", help="print the receipt as JSON")
    applied = commands.add_parser("apply", help="dry-run, then replace changed outputs atomically")
    applied.add_argument("--receipt", action="store_true", help="print the receipt as JSON")
    args = parser.parse_args(argv)

    if args.command == "plan":
        for position, step, generator in plan():
            if generator is None:
                print(f"{position:>2}. {step}")
            else:
                locus = "" if generator.locus == "relocatable" else f"   [{generator.locus}]"
                print(f"{position:>2}. scripts/{step}.py --write{locus}")
        return 0
    if args.command == "describe":
        print(json.dumps(describe(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "bundle-inputs":
        for path in bundle_input_contracts():
            print(path)
        return 0

    keep = args.command == "apply"
    transaction = dry_run(keep=keep)
    written: tuple[str, ...] = ()
    try:
        if args.command == "apply":
            written = apply_transaction(WORKSPACE, transaction)
    finally:
        if keep:
            _remove_tree(WORKSPACE, tolerant=True)
    if args.receipt:
        print(
            json.dumps(receipt(transaction, written), ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        _report(transaction, written)
    return 1 if transaction.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
