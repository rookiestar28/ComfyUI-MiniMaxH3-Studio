"""Generate the M19 closeout matrix by re-deriving every number from the committed artifacts.

The M19 chain wrote five records full of numbers. None of them is the source here. Every reading is
read out of the artifact that checkpoint actually committed, with `git show`, and recomputed --
because a record's prose is not evidence, and M18-06's closeout log records the one metric it
copied from prose being wrong.

That also means this generator answers a question the records cannot: whether a value that was
supposed to stay fixed across five items actually did. A record can only say what its own item saw.

Reads git objects as data and executes nothing from them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.governance.m19_closeout import (  # noqa: E402
    ABSENT,
    Checkpoint,
    CloseoutRow,
    M19Closeout,
    M19CloseoutError,
    Reading,
    RowExpectation,
)

ARTIFACT_PATH = Path("governance/contracts/m19_closeout_v1.json")

# CRITICAL: these four paths address the accepted M19-01..M19-05 checkpoint *trees*, not the
# working tree. M23-55 moved three of the four artifacts out of the installed package; the
# probes must keep the packaged spelling because `git show <commit>:<path>` resolves inside a
# commit where that is where they lived. Re-pathing them turns every probe into ABSENT, which
# reads as a real regression rather than as a broken path.
# `scripts/governance/relocate_packaged_artifacts.py` declares three of them in
# `HISTORICAL_REFERENCES`; the fourth never moved.
ARCHITECTURE = "comfyui_h3_context/contracts/architecture_inventory_v1.json"
SURFACE = "comfyui_h3_context/contracts/public_surface_v1.json"
CONTRACTS = "comfyui_h3_context/contracts/contract_inventory_v1.json"
NODES = "comfyui_h3_context/contracts/node_surface_v1.json"
BUNDLE = "comfyui_h3_context/web/h3-context-sidebar.js"
BASELINE = "tests/acceptance_baseline.json"

#: The chain, in order, each at the commit that accepted it. These are the only remembered values
#: in this generator, and they are checkable: `git show <commit>` either produces the tree or fails.
CHECKPOINTS: tuple[tuple[str, str, str], ...] = (
    ("M19-01", "2fea0df", "architecture inventory and fitness baseline"),
    ("M19-02", "f3742ae", "public surface authority; two import-time payloads deferred"),
    ("M19-03", "e0d8979", "ComfyUI node adapter decomposition behind a node-surface record"),
    ("M19-04", "9da959f", "product shell frontend decomposition and its layering guard"),
    ("M19-05", "ab4c3cc", "three pure-core modules decomposed and their layering guard"),
)


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def _resolve(rev: str) -> str:
    return _git("rev-parse", f"{rev}^{{commit}}").decode().strip()


def _blob(commit: str, path: str) -> bytes | None:
    """The file's bytes at that commit, or None if it did not exist yet."""

    try:
        return _git("show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def _document(commit: str, path: str) -> Any:
    raw = _blob(commit, path)
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


# Each probe takes a resolved commit and returns the reading, or ABSENT.


def _architecture(field: Callable[[Any], str]) -> Callable[[str], str]:
    def probe(commit: str) -> str:
        document = _document(commit, ARCHITECTURE)
        return ABSENT if document is None else field(document)

    return probe


def _surface(field: Callable[[Any], str]) -> Callable[[str], str]:
    def probe(commit: str) -> str:
        document = _document(commit, SURFACE)
        return ABSENT if document is None else field(document)

    return probe


def _nodes(field: Callable[[Any], str]) -> Callable[[str], str]:
    def probe(commit: str) -> str:
        document = _document(commit, NODES)
        return ABSENT if document is None else field(document)

    return probe


def _pure_core(document: Any) -> Any:
    for authority in document["authorities"]:
        if authority["level"] == "pure_core":
            return authority
    raise M19CloseoutError("public surface record carries no pure_core authority")


def _baseline_ids(commit: str) -> tuple[str, ...]:
    document = _document(commit, CONTRACTS)
    if document is None:
        return ()
    return tuple(sorted({str(entry["contract_id"]) for entry in document["entries"]}))


def _retained(first_ids: tuple[str, ...]) -> Callable[[str], str]:
    """How many of the baseline's contract identities still exist at a checkpoint.

    Entry *count* grows across the chain -- M19-02 alone adds a schema and its record -- so a plain
    count says nothing about loss. This says what matters: no identity the baseline knew about
    stopped existing.
    """

    def probe(commit: str) -> str:
        present = set(_baseline_ids(commit))
        if not present:
            return ABSENT
        return str(sum(1 for identity in first_ids if identity in present))

    return probe


def _bundle(field: str) -> Callable[[str], str]:
    def probe(commit: str) -> str:
        raw = _blob(commit, BUNDLE)
        if raw is None:
            return ABSENT
        if field == "sha256":
            return "sha256:" + hashlib.sha256(raw).hexdigest()
        return str(len(raw))

    return probe


def _acceptance(key: str) -> Callable[[str], str]:
    def probe(commit: str) -> str:
        document = _document(commit, BASELINE)
        if document is None:
            return ABSENT
        return str(len(document[key]))

    return probe


def _runtime_cycles(document: Any) -> str:
    return str(sum(1 for cycle in document["cycles"] if cycle["runtime_cycle"]))


def _largest_module(document: Any) -> str:
    return str(max(int(module["lines"]) for module in document["modules"]))


def _largest_authored_module(document: Any) -> str:
    """The largest module anybody wrote, which is not the same as the largest module.

    `core.__init__` is 4 400-odd lines of generated re-exports and is the biggest file in the
    package at every checkpoint, so the plain maximum barely moves and reports nothing about the
    chain. Excluding what a generator wrote is what makes the number mean "the largest thing a
    reader has to hold in their head".
    """

    authored = [
        int(module["lines"])
        for module in document["modules"]
        if not str(module["module"]).endswith("__init__")
    ]
    return str(max(authored))


def _largest_frontend_module(pattern: str) -> Callable[[str], str]:
    """The largest TypeScript module under `frontend/src/host/`.

    The Python inventory cannot see the frontend, so M19-04's decomposition is invisible to every
    architecture row above. This reads the tree directly: `git ls-tree -l` gives each blob's size
    without fetching it, and byte size is the comparable measure here because line counts would
    need every blob read.
    """

    def probe(commit: str) -> str:
        try:
            listing = _git("ls-tree", "-l", commit, "frontend/src/host/").decode("utf-8")
        except subprocess.CalledProcessError:  # pragma: no cover - the path has always existed
            return ABSENT
        sizes = [
            int(parts[3])
            for line in listing.splitlines()
            if (parts := line.split(maxsplit=4)) and len(parts) == 5 and parts[1] == "blob"
            if parts[4].endswith(".ts") and parts[4].rsplit("/", 1)[-1].startswith(pattern)
        ]
        return str(max(sizes)) if sizes else ABSENT

    return probe


def _rows() -> list[tuple[str, str, str, str, RowExpectation, Callable[[str], str]]]:
    first_ids = _baseline_ids(_resolve(CHECKPOINTS[0][1]))
    fixed = RowExpectation.CONSTANT
    noted = RowExpectation.INFORMATIONAL
    return [
        # Architecture: the three that must never move, and two that were meant to.
        (
            "architecture",
            "forbidden_imports",
            ARCHITECTURE,
            "M19-01",
            fixed,
            _architecture(lambda d: str(len(d["forbidden_imports"]))),
        ),
        (
            "architecture",
            "layer_inversions",
            ARCHITECTURE,
            "M19-01",
            fixed,
            _architecture(lambda d: str(len(d["layer_inversions"]))),
        ),
        (
            "architecture",
            "runtime_cycles",
            ARCHITECTURE,
            "M19-01",
            fixed,
            _architecture(_runtime_cycles),
        ),
        (
            "architecture",
            "modules",
            ARCHITECTURE,
            "M19-01",
            noted,
            _architecture(lambda d: str(len(d["modules"]))),
        ),
        (
            "architecture",
            "largest_module_lines",
            ARCHITECTURE,
            "M19-01",
            noted,
            _architecture(_largest_module),
        ),
        (
            "architecture",
            "largest_authored_module_lines",
            ARCHITECTURE,
            "M19-01",
            noted,
            _architecture(_largest_authored_module),
        ),
        # Surface: the promise the package makes.
        (
            "surface",
            "pure_core_declared",
            SURFACE,
            "M19-02",
            fixed,
            _surface(lambda d: str(_pure_core(d)["declared"])),
        ),
        (
            "surface",
            "pure_core_names_digest",
            SURFACE,
            "M19-02",
            fixed,
            _surface(lambda d: str(_pure_core(d)["names_digest"])),
        ),
        (
            "surface",
            "shadowed_exports",
            SURFACE,
            "M19-02",
            fixed,
            _surface(lambda d: str(len(d["shadowed"]))),
        ),
        # Contracts: identity, not count.
        (
            "contracts",
            "baseline_identities_retained",
            CONTRACTS,
            "M19-01",
            fixed,
            _retained(first_ids),
        ),
        # Host: everything a ComfyUI host reads off these node classes.
        (
            "host",
            "node_surface_fingerprint",
            NODES,
            "M19-03",
            fixed,
            _nodes(lambda d: str(d["fingerprint"])),
        ),
        ("host", "nodes", NODES, "M19-03", fixed, _nodes(lambda d: str(len(d["nodes"])))),
        (
            "host",
            "exported_names",
            NODES,
            "M19-03",
            fixed,
            _nodes(lambda d: str(len(d["exported_names"]))),
        ),
        # Frontend: the artifact users receive. It moved at M19-04 and the record says why.
        ("frontend", "bundle_sha256", BUNDLE, "M19-01", noted, _bundle("sha256")),
        ("frontend", "bundle_bytes", BUNDLE, "M19-01", noted, _bundle("bytes")),
        # Two rows, deliberately. The first is what M19-04 moved; the second is what it left, and
        # `appMode.ts` at 120 kB is why AC-M19-04-04 had to be corrected from "the largest frontend
        # host module" to "the largest module in the frozen cohort".
        (
            "frontend",
            "largest_graph_module_bytes",
            "frontend/src/host",
            "M19-01",
            noted,
            _largest_frontend_module("graph"),
        ),
        (
            "frontend",
            "largest_host_module_bytes",
            "frontend/src/host",
            "M19-01",
            noted,
            _largest_frontend_module(""),
        ),
        # Acceptance: the registered criteria and the gate-enforced ABI.
        ("acceptance", "registered_criteria", BASELINE, "M19-01", fixed, _acceptance("criteria")),
        (
            "acceptance",
            "public_python_abi",
            BASELINE,
            "M19-01",
            fixed,
            _acceptance("public_python_abi"),
        ),
    ]


def build_closeout() -> M19Closeout:
    resolved = [(item, _resolve(rev), summary) for item, rev, summary in CHECKPOINTS]
    checkpoints = tuple(
        Checkpoint(item=item, commit=commit, summary=summary) for item, commit, summary in resolved
    )
    rows = tuple(
        CloseoutRow(
            domain=domain,
            metric=metric,
            source=source,
            introduced_by=introduced,
            expectation=expectation,
            readings=tuple(
                Reading(item=item, value=probe(commit)) for item, commit, _summary in resolved
            ),
        )
        for domain, metric, source, introduced, expectation, probe in _rows()
    )
    return M19Closeout(checkpoints=checkpoints, rows=rows)


def artifact_bytes(closeout: M19Closeout) -> bytes:
    document = json.dumps(closeout.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the M19 closeout matrix.")
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        closeout = build_closeout()
    except (M19CloseoutError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(closeout)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(json.dumps({"status": "FAIL", "detail": "closeout artifact is stale"}))
        return 1
    broken = [row.metric for row in closeout.broken]
    print(
        json.dumps(
            {"status": "FAIL" if broken else "PASS", **closeout.to_public_dict()},
            ensure_ascii=False,
        )
    )
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
