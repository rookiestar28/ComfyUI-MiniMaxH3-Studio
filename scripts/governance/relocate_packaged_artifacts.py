"""M23-55: move the governance-only packaged artifacts out of the installed package.

`comfyui_h3_context/contracts/` ships every JSON this repository generates, but only nine of them
have a reader inside the shipped product: five the installed package opens at run time and four the
frontend bundle compiles in. The rest are read by `scripts/`, `tests/` and evidence tooling, so they
inflate the wheel and misdescribe what the runtime needs. This tool moves exactly those to
`governance/contracts/`, which sits outside `comfyui_h3_context*` and is therefore excluded from the
wheel, the sdist and the Registry archive by the existing packaging rules.

CRITICAL: the move set is **read from the measured readership record**, never guessed from a name.
`host_seam_census_v1.json` is the live proof that heuristics get this wrong -- it looks exactly like
a governance census and `frontend/src` imports it, so moving it would silently break the bundle.
`scripts/packaged_artifact_readership.py` computes the classification and this tool consumes it;
a selected path that appears in `derived_artifacts.py bundle-inputs` or in a reading class is a
refusal, not a case to whitelist around.

Two reference forms have to be rewritten and both are load-bearing:

- the literal path, with `/`, `\\` or an escaped `\\\\` separator (`.secrets.baseline` keys use the
  escaped Windows form, so a forward-slash-only rewrite leaves seven stale keys behind);
- the joined-segment form, `"comfyui_h3_context" / "contracts" / "<name>"` or the same with commas,
  which `ruff format` freely breaks across lines.

A third form cannot be rewritten mechanically at all: a module that binds the contracts directory to
a constant and joins a basename later. Those are reported as `manual` rows by `dry-run` and `apply`
refuses while any remain, because a partly rewritten reference graph is not a working tree.

Usage:
    python scripts/governance/relocate_packaged_artifacts.py dry-run
    python scripts/governance/relocate_packaged_artifacts.py apply

The relocation this tool exists for has been applied, so both commands now exit 2 with "the
readership record selects nothing to move": the record is regenerated afterwards and then
classifies only the nine retained artifacts. That is the expected terminal state, not a fault.
The tool stays in the tree because its three judgement tables are the record of why each
excepted file is excepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.derived_artifacts import _is_link  # noqa: E402
from scripts.packaged_artifact_readership import ARTIFACT_PATH as _READERSHIP_ARTIFACT_PATH

ROOT = Path(__file__).resolve().parents[2]

PACKAGE_ROOT = "comfyui_h3_context/contracts"
GOVERNANCE_ROOT = "governance/contracts"
PACKAGED_ARTIFACT_ROOTS = (PACKAGE_ROOT, "comfyui_h3_context/fonts")
_RETAINED_PACKAGE_ROOTS = PACKAGED_ARTIFACT_ROOTS[1:]

# CRITICAL: this is the generator's own output path, imported rather than restated. It was a
# `PACKAGE_ROOT` literal once, and the relocation moved the record out from under it -- the tool
# then refused every invocation with "the readership record is missing", including the `dry-run`
# its own docstring tells you to run. `SELF_DESCRIBING` is why the mechanical rewrite did not fix
# it: that set exists to protect the `HISTORICAL_REFERENCES` values below, which must keep the
# packaged spelling forever, and excluding the whole file took this constant with them. A record
# that must move and a record that must never move cannot both be defended by excluding a file,
# so the one that moves is not spelled here at all -- it follows whatever
# `scripts/packaged_artifact_readership.py` writes.
READERSHIP = _READERSHIP_ARTIFACT_PATH.as_posix()

#: Dispositions in the readership record that this item relocates. Plan decision B moves the
#: `no_reader` rows with the governance set: nothing is deleted before the initial release, the
#: retirement census keeps listing them, and the retirement decision itself is a later item.
MOVED_DISPOSITIONS = ("movable", "retain pending retirement review")

#: Readership classes that must never move. Both are proofs of a reader inside the shipped product.
READING_CLASSES = ("runtime_read", "frontend_source_read")

#: Suffixes never scanned for references. Rewriting inside them would corrupt them.
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".mp4",
        ".mov",
        ".webm",
        ".avi",
        ".wav",
        ".mp3",
        ".flac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".gz",
        ".zip",
        ".whl",
        ".safetensors",
        ".ckpt",
        ".pth",
        ".pt",
        ".bin",
        ".onnx",
        ".pyc",
        ".pyo",
        ".pdf",
    }
)

#: Bound on a single scanned file. Nothing tracked in this repository approaches it; a file that
#: does is reported rather than loaded, so a hostile or accidental giant cannot exhaust memory.
MAX_SCAN_BYTES = 8 * 1024 * 1024

#: References that address a **historical** tree and must keep the packaged path forever.
#:
#: CRITICAL: `git show <commit>:<path>` and `git ls-tree <commit> <path>` resolve the path inside
#: that commit, where the artifact was still packaged. Rewriting one of these produces a tool that
#: raises `CalledProcessError` on a path that never existed at that commit -- and the two generators
#: below are `in_repository`, so the failure only appears when the full cascade runs, not in any
#: focused check. Each entry is matched exactly once; a declaration that stops matching refuses,
#: so this table cannot rot into a silent no-op.
HISTORICAL_REFERENCES: dict[str, tuple[str, ...]] = {
    # Reads the M18-01 and M18-05 checkpoint trees.
    "scripts/closeout_matrix.py": (
        'DOMAIN = "comfyui_h3_context/contracts/fingerprint_domain_v1.json"',
        'git_show(M18_05, "comfyui_h3_context/contracts/retirement_eligibility_v1.json")',
    ),
    # Probes each accepted M19-01..M19-05 checkpoint tree.
    "scripts/m19_closeout.py": (
        'ARCHITECTURE = "comfyui_h3_context/contracts/architecture_inventory_v1.json"',
        'SURFACE = "comfyui_h3_context/contracts/public_surface_v1.json"',
        'NODES = "comfyui_h3_context/contracts/node_surface_v1.json"',
    ),
}

_MASK = "\x00historical\x00"


class RelocationError(RuntimeError):
    """A refusal. Raised before any filesystem mutation whenever possible."""


@dataclass(frozen=True, slots=True)
class Rewrite:
    """One tracked file whose bytes change, addressed by its path before the move."""

    path: str
    #: Where the rewritten bytes must be written. Differs from `path` for a moved file.
    destination: str
    count: int
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True, slots=True)
class Plan:
    moves: tuple[tuple[str, str], ...] = ()
    rewrites: tuple[Rewrite, ...] = ()
    #: Files that reference the packaged contracts directory without an inline basename while also
    #: naming a moved artifact. A human decides what the constant should be; this tool will not.
    manual: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()

    @property
    def rewritten_count(self) -> int:
        return sum(rewrite.count for rewrite in self.rewrites)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )


def _tracked_files() -> tuple[str, ...]:
    result = _run(["git", "ls-files", "-z"])
    if result.returncode != 0:
        raise RelocationError("git ls-files failed; this must run inside the repository")
    return tuple(sorted(entry for entry in result.stdout.split("\0") if entry))


def _read_record() -> dict[str, object]:
    path = ROOT / READERSHIP
    if not path.is_file():
        raise RelocationError(f"the readership record is missing: {READERSHIP}")
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _bundle_inputs() -> frozenset[str]:
    """The paths `frontend/src` compiles in, asked of the declaration rather than assumed."""

    result = _run([sys.executable, "scripts/derived_artifacts.py", "bundle-inputs"])
    if result.returncode != 0:
        raise RelocationError("derived_artifacts.py bundle-inputs failed; cannot verify the split")
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def selected_paths(record: dict[str, object] | None = None) -> tuple[str, ...]:
    """The relocation set, computed from the record at call time and cross-checked twice.

    CRITICAL: the two cross-checks below are not belt-and-braces. The readership record and the
    generator declaration are produced by different scripts from different evidence -- one greps for
    readers, the other declares bundle inputs -- so an artifact that becomes a bundle input without
    the census noticing is caught here and nowhere else before the offline rebuild parity in the
    Full Gate, hours later.
    """

    record = record if record is not None else _read_record()
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RelocationError("the readership record carries no artifact rows")

    selected: list[str] = []
    reading: set[str] = set()
    seen_paths: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict):
            raise RelocationError("the readership record holds a non-object artifact row")
        path = row.get("path")
        disposition = row.get("disposition")
        readership = row.get("readership")
        if not isinstance(path, str) or not isinstance(disposition, str):
            raise RelocationError("an artifact row is missing its path or disposition")
        _reject_unsafe(path)
        if path in seen_paths:
            raise RelocationError("the readership record names an artifact twice")
        seen_paths.add(path)
        if not path.startswith(f"{PACKAGE_ROOT}/"):
            retained_root = next(
                (root for root in _RETAINED_PACKAGE_ROOTS if path.startswith(f"{root}/")),
                None,
            )
            if retained_root is not None:
                # CRITICAL: the readership census covers every packaged artifact root, while this
                # migration moves contracts only. Ignore another root solely when every row there
                # is reader-backed and retained; otherwise a new orphan could evade relocation.
                moves = disposition.split(":")[0].strip() in MOVED_DISPOSITIONS
                if readership in READING_CLASSES and not moves:
                    continue
                if readership in READING_CLASSES:
                    raise RelocationError(
                        "refusing: an artifact in a retained packaged root has a shipped reader "
                        f"and cannot move: {path}"
                    )
                raise RelocationError(
                    "refusing: an artifact in a retained packaged root lacks a shipped reader: "
                    + path
                )
            raise RelocationError(f"the record names {path}, which is outside {PACKAGE_ROOT}/")
        if readership in READING_CLASSES:
            reading.add(path)
        if disposition.split(":")[0].strip() in MOVED_DISPOSITIONS:
            selected.append(path)

    overlap = sorted(set(selected) & reading)
    if overlap:
        raise RelocationError(
            "refusing: these have a reader inside the shipped product and cannot move: "
            + ", ".join(overlap)
        )
    inputs = _bundle_inputs()
    overlap = sorted(set(selected) & inputs)
    if overlap:
        raise RelocationError(
            "refusing: these are declared bundle inputs and cannot move: " + ", ".join(overlap)
        )
    # A selected path is allowed to be absent from the package *when it is already at its
    # destination*: that is the half-applied tree, which `_state` diagnoses with a recovery
    # instruction. Refusing here instead would report a missing file and hide why it is missing.
    missing = sorted(
        path
        for path in selected
        if not (ROOT / path).is_file()
        and not (ROOT / GOVERNANCE_ROOT / path.rsplit("/", 1)[1]).is_file()
    )
    if missing:
        raise RelocationError(
            "refusing: selected paths absent from the tree: " + ", ".join(missing)
        )
    linked = sorted(path for path in selected if (ROOT / path).exists() and _is_link(ROOT / path))
    if linked:
        raise RelocationError("refusing: selected paths are links, not files: " + ", ".join(linked))
    return tuple(sorted(selected))


def _reject_unsafe(path: str) -> None:
    if not path or path.startswith("/") or ":" in path:
        raise RelocationError(f"refusing an absolute or drive-qualified path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RelocationError(f"refusing a traversing or empty path segment: {path!r}")


def _name_alternation(names: frozenset[str]) -> str:
    # CRITICAL: an empty set would produce the empty alternation, and `(?:)` matches the empty
    # string -- the rewrite pattern would then match every `comfyui_h3_context/contracts/` prefix
    # in the repository and re-path all nine retained artifacts. `build_plan` refuses before it
    # reaches here, and this is the second lock on the same door: a regex that matches everything
    # is never the right answer to "which names moved".
    if not names:
        raise RelocationError("refusing to build a rewrite pattern from an empty name set")
    # Longest first so `x_v1.schema.json` is never split by a shorter sibling.
    ordered = sorted(names, key=lambda name: (-len(name), name))
    return "|".join(re.escape(name) for name in ordered)


#: What may sit between two quoted path segments. `ruff format` breaks these across lines freely,
#: and `Path("comfyui_h3_context") / "contracts"` closes the call between the first two segments, so
#: an optional bracket has to be allowed. It is captured and re-emitted verbatim rather than
#: rebuilt, which is what keeps the rewrite formatting-neutral.
_SEGMENT_JOIN = r"\s*\)?\s*[,/]\s*"


def _patterns(names: frozenset[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    alternation = _name_alternation(names)
    boundary = r"(?![A-Za-z0-9_.\-])"
    literal = re.compile(
        r"comfyui_h3_context(?P<sep>/|\\{1,2})contracts(?P=sep)"
        rf"(?P<name>{alternation}){boundary}"
    )
    joined = re.compile(
        r"(?P<q1>[\"'])comfyui_h3_context(?P=q1)"
        rf"(?P<s1>{_SEGMENT_JOIN})"
        r"(?P<q2>[\"'])contracts(?P=q2)"
        rf"(?=(?P<s2>{_SEGMENT_JOIN})(?P<q3>[\"'])"
        rf"(?P<name>{alternation})(?P=q3))"
    )
    return literal, joined


def _rewrite_text(text: str, literal: re.Pattern[str], joined: re.Pattern[str]) -> tuple[str, int]:
    count = 0

    def replace_literal(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        separator = match.group("sep")
        return f"governance{separator}contracts{separator}{match.group('name')}"

    def replace_joined(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        quote_one = match.group("q1")
        quote_two = match.group("q2")
        return f"{quote_one}governance{quote_one}{match.group('s1')}{quote_two}contracts{quote_two}"

    text = literal.sub(replace_literal, text)
    text = joined.sub(replace_joined, text)
    return text, count


def _mask_historical(tracked: str, text: str) -> tuple[str, tuple[str, ...]]:
    """Replace this file's declared historical references with an inert placeholder.

    Returns the masked text and the spans in the order they must be restored.
    """

    declared = HISTORICAL_REFERENCES.get(tracked)
    if not declared:
        return text, ()
    for span in declared:
        if text.count(span) != 1:
            raise RelocationError(
                f"{tracked}: the declared historical reference {span!r} appears "
                f"{text.count(span)} times, not once. Re-read the file before relocating."
            )
        text = text.replace(span, _MASK, 1)
    return text, declared


def _unmask_historical(text: str, spans: tuple[str, ...]) -> str:
    for span in spans:
        text = text.replace(_MASK, span, 1)
    return text


def _without_inline_paths(text: str) -> str:
    """`text` with every fully-qualified contract path removed, under either root.

    What is left is the vocabulary a per-basename rewrite cannot reach: directory constants, and
    basenames that some other expression joins to one of them.
    """

    flat = re.sub(r"\s*\n\s*", " ", text)
    for root in ("comfyui_h3_context", "governance"):
        flat = re.sub(rf"{root}(/|\\{{1,2}})contracts\1[A-Za-z0-9_.\-]+\.json", "", flat)
        flat = re.sub(
            rf"([\"']){root}\1{_SEGMENT_JOIN}([\"'])contracts\2{_SEGMENT_JOIN}"
            r"([\"'])[A-Za-z0-9_.\-]+\.json\3",
            "",
            flat,
        )
    return flat


def _bare_moved_names(text: str, names: frozenset[str]) -> frozenset[str]:
    """Moved basenames the file mentions outside any fully-qualified path.

    Empty unless the file also anchors on the packaged contracts directory: a basename alone is
    just a string, and only a packaged-root expression next to it can still resolve to the old
    location after the rewrite.
    """

    # GUARD: the anchor is read from the residue on purpose. What survives `_without_inline_paths`
    # is a *directory* mention -- `CONTRACTS = ROOT / "comfyui_h3_context" / "contracts"` and its
    # kin -- because every complete three-segment path was deleted. Anchoring on the whole file
    # instead reports every module that merely spells a packaged path correctly, 76 files at the
    # pre-move commit, and a check that loud is a check nobody reads. The narrower case where a
    # complete path is itself the anchor is `_derived_sibling_names` below, not this function.
    residue = _without_inline_paths(text)
    names_packaged_root = bool(
        re.search(r"comfyui_h3_context(/|\\{1,2})contracts", residue)
        or re.search(rf"([\"'])comfyui_h3_context\1{_SEGMENT_JOIN}([\"'])contracts\2", residue)
    )
    if not names_packaged_root:
        return frozenset()
    return frozenset(name for name in names if name in residue)


#: `Path` expressions that build a new path out of an existing one. The name they produce never
#: appears in the source, so no per-path rewrite can reach it.
_SIBLING_DERIVATION = re.compile(
    r"(?:\.with_name\(|\.with_suffix\(|\.parent\s*/\s*)\s*[\"'](?P<name>[A-Za-z0-9_.\-]+\.json)[\"']"
)


def _derived_sibling_names(text: str, names: frozenset[str]) -> frozenset[str]:
    """Moved basenames this file builds as a sibling of another path rather than spelling out.

    CRITICAL: this is the hole that let the tool report a clean tree and ship a broken one.
    `_bare_moved_names` deliberately anchors on a surviving *directory* mention, and
    `tests/test_hc_09_host_seam_contract.py` had none: its anchor was a complete path to a
    *retained* artifact, which `_without_inline_paths` strips, and its two schemas were
    `CENSUS_PATH.with_name("host_seam_census_v1.schema.json")`. The relocation left both pointing
    into the emptied package and only the Full Gate found it, because the string
    `comfyui_h3_context/contracts/host_seam_census_v1.schema.json` appears nowhere in the file for
    a rewrite to match. Any `with_name`, `with_suffix` or `.parent /` naming a moved basename is
    therefore reported for hand re-pathing regardless of what the anchor looks like -- a document
    and its schema may now live under different roots, so a sibling is no longer a safe assumption.
    """

    return frozenset(
        match.group("name")
        for match in _SIBLING_DERIVATION.finditer(text)
        if match.group("name") in names
    )


#: The relocation itself: neither rewritten nor scanned for stale references.
#:
#: CRITICAL: this is not a convenience. `HISTORICAL_REFERENCES` above stores the *old* path of five
#: spans that must never move, as string literals -- so a tool that rewrites its own source turns
#: those declarations into paths that match nothing, and the guard they encode silently stops
#: guarding. This module and its test name both roots and a dozen moved basenames because that is
#: their subject, the same shape as `SELF_DESCRIBING_PATHS` in `scripts/contract_inventory.py`:
#: a record *about* the contracts is not a *consumer* of them.
SELF_DESCRIBING = frozenset(
    {
        "scripts/governance/relocate_packaged_artifacts.py",
        "tests/test_m23_55_relocation.py",
    }
)

#: Files a `manual` detector reports that were read by hand and found correct, with the exact moved
#: basenames each is allowed to name. Two shapes reach here, and each entry says which it is:
#: a file that keeps a packaged-contracts constant while naming a moved artifact, and a file that
#: builds a path as a sibling of another path. The value is not a mute button but a pin, so a
#: *different* moved basename appearing in one of them later still refuses, and anything not listed
#: here refuses outright.
ACKNOWLEDGED_SPLIT_ROOTS: dict[str, frozenset[str]] = {
    # Declares both roots: four outputs stay in the package, twelve move.
    "scripts/derived_artifacts.py": frozenset(
        {
            "architecture_fitness_v1.json",
            "architecture_inventory_v1.json",
            "closeout_matrix_v1.json",
            "fingerprint_domain_v1.json",
            "m19_closeout_v1.json",
            "managed_run_stategraph_v1.json",
            "node_surface_v1.json",
            "packaged_artifact_readership_v1.json",
            "public_surface_v1.json",
            "retirement_eligibility_v1.json",
            "sbom.spdx.json",
            "supply_chain_v1.json",
        }
    ),
    # Classifies the packaged directory, so it must keep naming it; its own output moves.
    "scripts/packaged_artifact_readership.py": frozenset({"packaged_artifact_readership_v1.json"}),
    # Scans both roots after the move, and the census it reports on stays in the package.
    "scripts/contract_inventory.py": frozenset(),
    # Reads the retained artifact from the package and every schema below from the governance root.
    "tests/test_cross_language_surface.py": frozenset(
        {
            "cross_language_surface_v1.schema.json",
            "product_shell_v1.schema.json",
            "sidebar_workspace_v1.schema.json",
        }
    ),
    # Prose. It names the packaged directory and two artifacts by basename; AGENTS.md section 5.1
    # keeps a prose document out of the test process entirely, so nothing here resolves a path.
    "tests/TEST_SOP.md": frozenset({"architecture_fitness_v1.json", "supply_chain_v1.json"}),
    # Reads the retained census and the moved probe policy.
    "tests/test_hc_09_host_seam_probe_policy.py": frozenset({"host_seam_probe_policy_v1.json"}),
    # Reads the retained profile artifact and its moved schema.
    "tests/test_installation_profiles.py": frozenset({"installation_profiles_v1.schema.json"}),
    # Asserts that the packaged directory ships nothing the wheel does not carry.
    "tests/test_build_gate_report.py": frozenset(),
    # Pins which self-describing records sit under which root.
    "tests/test_tooling_retirement.py": frozenset(),
    # Sibling derivation, checked and correct: `ARTIFACT` is the moved
    # `governance/contracts/assisted_authoring_compatibility_v1.json`, so `with_name` lands beside
    # it under the same root. Pinned rather than left silent because the anchor and the sibling are
    # only correct together -- move one and the other follows into a file that does not exist.
    "tests/test_m22_16_compatibility_closeout.py": frozenset(
        {"assisted_authoring_compatibility_v1.schema.json"}
    ),
    # Sibling derivation, checked and correct: `self.ARTIFACT` is the moved
    # `governance/contracts/managed_run_stategraph_v1.json`, and the schema moved with it.
    "tests/test_managed_run_aggregate.py": frozenset({"managed_run_stategraph_v1.schema.json"}),
    # Joined constant, checked and correct. The anchor is `_schema_files_at`'s bare
    # `"comfyui_h3_context/contracts/"` prefix, which lists that directory *inside an M18
    # checkpoint commit* and must stay packaged; the bare basename is prose in the module comment
    # naming the masked historical span. Neither can be re-pathed and neither is joined to the
    # other, but the pairing is exactly what the detector looks for, so it is pinned to that one
    # name rather than silenced.
    "scripts/closeout_matrix.py": frozenset({"retirement_eligibility_v1.json"}),
}


def build_plan() -> Plan:
    record = _read_record()
    selected = selected_paths(record)
    if not selected:
        # CRITICAL: refuse here rather than rendering a plan of zero moves. The record is
        # regenerated after the relocation and then lists only the retained artifacts, so this is
        # the tool's ordinary terminal state -- but a zero-move plan still builds rewrite patterns
        # from an empty name set, and `dry-run` reported nine phantom rewrites against the nine
        # retained paths before this guard existed. `apply` was already safe, refusing on the same
        # condition one step later; the two now answer identically.
        raise RelocationError(
            "the readership record selects nothing to move. Either the relocation already "
            "happened -- the record then classifies only the retained set, which is the expected "
            "state after this item -- or the record is stale and needs regenerating."
        )
    names = frozenset(path.rsplit("/", 1)[1] for path in selected)
    literal, joined = _patterns(names)

    moves = tuple((path, GOVERNANCE_ROOT + "/" + path.rsplit("/", 1)[1]) for path in selected)
    destinations = {source: destination for source, destination in moves}

    rewrites: list[Rewrite] = []
    manual: list[str] = []
    for tracked in _tracked_files():
        path = ROOT / tracked
        if Path(tracked).suffix.lower() in BINARY_SUFFIXES:
            continue
        if not path.is_file() or _is_link(path):
            continue
        if tracked in SELF_DESCRIBING:
            continue
        if path.stat().st_size > MAX_SCAN_BYTES:
            raise RelocationError(f"refusing to scan {tracked}: larger than {MAX_SCAN_BYTES} bytes")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        masked, spans = _mask_historical(tracked, text)
        rewritten, count = _rewrite_text(masked, literal, joined)
        rewritten = _unmask_historical(rewritten, spans)
        if count:
            rewrites.append(
                Rewrite(
                    path=tracked,
                    destination=destinations.get(tracked, tracked),
                    count=count,
                    before_sha256=_digest(raw),
                    after_sha256=_digest(rewritten.encode("utf-8")),
                )
            )
        acknowledged = ACKNOWLEDGED_SPLIT_ROOTS.get(tracked) or frozenset()
        pinned = ACKNOWLEDGED_SPLIT_ROOTS.get(tracked) is not None
        bare = _bare_moved_names(rewritten, names)
        if bare and (not pinned or bare - acknowledged):
            unexpected = sorted(bare if not pinned else bare - acknowledged)
            manual.append(f"{tracked}: joins a packaged constant to {', '.join(unexpected)}")
        derived = _derived_sibling_names(rewritten, names) - acknowledged
        if derived:
            manual.append(f"{tracked}: derives sibling {', '.join(sorted(derived))}")

    retained = tuple(
        sorted(
            entry
            for entry in _tracked_files()
            if entry.startswith(f"{PACKAGE_ROOT}/") and entry not in destinations
        )
    )
    return Plan(
        moves=moves,
        rewrites=tuple(rewrites),
        manual=tuple(sorted(manual)),
        retained=retained,
    )


def _state(plan: Plan) -> str:
    """`pristine`, `applied`, or a description of a half-applied tree."""

    sources_present = [source for source, _ in plan.moves if (ROOT / source).exists()]
    destinations_present = [
        destination for _, destination in plan.moves if (ROOT / destination).exists()
    ]
    if len(sources_present) == len(plan.moves) and not destinations_present:
        return "pristine"
    if not sources_present and len(destinations_present) == len(plan.moves):
        return "applied"
    return (
        f"half-applied: {len(sources_present)} of {len(plan.moves)} sources still present, "
        f"{len(destinations_present)} destinations already present"
    )


def render(plan: Plan) -> str:
    lines = [
        f"relocation set: {len(plan.moves)} files -> {GOVERNANCE_ROOT}/",
        f"retained in {PACKAGE_ROOT}/: {len(plan.retained)} files",
        f"reference rewrites: {plan.rewritten_count} in {len(plan.rewrites)} files",
        f"tree state: {_state(plan)}",
    ]
    for entry in plan.retained:
        lines.append(f"  retained  {entry}")
    for rewrite in sorted(plan.rewrites, key=lambda item: (-item.count, item.path)):
        moved = " (moves)" if rewrite.destination != rewrite.path else ""
        lines.append(f"  rewrite {rewrite.count:5d}  {rewrite.path}{moved}")
    if plan.manual:
        lines.append("")
        lines.append(
            f"MANUAL: {len(plan.manual)} files bind the packaged contracts directory to a constant "
            "and join a moved basename later. Re-path them by hand before apply:"
        )
        for entry in plan.manual:
            lines.append(f"  manual    {entry}")
    return "\n".join(lines)


def apply_plan(plan: Plan) -> None:
    if plan.manual:
        raise RelocationError(
            "refusing to apply: "
            + ", ".join(plan.manual)
            + " still bind the packaged contracts directory to a constant. A half-rewritten "
            "reference graph is not a working tree."
        )
    if not plan.moves:
        raise RelocationError(
            "refusing to apply: the readership record selects nothing to move. Either the "
            "relocation already happened -- the record then classifies only the retained set -- "
            "or the record is stale and needs regenerating."
        )
    state = _state(plan)
    if state == "applied":
        raise RelocationError("refusing to apply: the relocation is already in place")
    if state != "pristine":
        raise RelocationError(
            f"refusing to apply over a {state}. Recover the prior tree with "
            "`git checkout -- .` and `git status` before retrying; this tool never resumes."
        )

    (ROOT / GOVERNANCE_ROOT).mkdir(parents=True, exist_ok=True)
    for source, destination in plan.moves:
        result = _run(["git", "mv", source, destination])
        if result.returncode != 0:
            raise RelocationError(
                f"git mv {source} -> {destination} failed: {result.stderr.strip()}"
            )

    names = frozenset(source.rsplit("/", 1)[1] for source, _ in plan.moves)
    literal, joined = _patterns(names)
    for rewrite in plan.rewrites:
        target = ROOT / rewrite.destination
        raw = target.read_bytes()
        if _digest(raw) != rewrite.before_sha256:
            raise RelocationError(
                f"refusing to write {rewrite.destination}: it changed since the plan was computed"
            )
        temporary = target.with_name(target.name + ".m23-55-tmp")
        masked, spans = _mask_historical(rewrite.path, raw.decode("utf-8"))
        rewritten, count = _rewrite_text(masked, literal, joined)
        rewritten = _unmask_historical(rewritten, spans)
        if count != rewrite.count:
            raise RelocationError(
                f"refusing to write {rewrite.destination}: the plan predicted {rewrite.count} "
                f"rewrites and {count} were found"
            )
        payload = rewritten.encode("utf-8")
        if _digest(payload) != rewrite.after_sha256:
            raise RelocationError(
                f"refusing to write {rewrite.destination}: the result differs from the plan"
            )
        # GUARD: write a sibling then `os.replace`, never truncate in place. An interrupted
        # in-place write leaves a half-written source file that no `git checkout` diagnoses as
        # suspicious, and this loop touches well over a hundred files.
        temporary.write_bytes(payload)
        os.replace(temporary, target)

    residue = surviving_references(frozenset(source for source, _ in plan.moves))
    if residue:
        raise RelocationError(
            "the rewrite left stale references behind: "
            + ", ".join(f"{path} ({count})" for path, count in residue)
        )


def surviving_references(selected: frozenset[str]) -> tuple[tuple[str, int], ...]:
    """Tracked files that still name a moved artifact under the packaged path."""

    names = frozenset(path.rsplit("/", 1)[1] for path in selected)
    literal, joined = _patterns(names)
    found: list[tuple[str, int]] = []
    for tracked in _tracked_files():
        path = ROOT / tracked
        if Path(tracked).suffix.lower() in BINARY_SUFFIXES:
            continue
        if not path.is_file() or _is_link(path) or tracked in SELF_DESCRIBING:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        text, _ = _mask_historical(tracked, text)
        count = len(literal.findall(text)) + len(joined.findall(text))
        if count:
            found.append((tracked, count))
    return tuple(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("dry-run", "apply"))
    arguments = parser.parse_args(argv)
    try:
        plan = build_plan()
        print(render(plan))
        if arguments.command == "apply":
            apply_plan(plan)
            print("\napplied")
    except RelocationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
