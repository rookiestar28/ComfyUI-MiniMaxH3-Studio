"""Generate and verify the deterministic contract-surface inventory for M18-01.

The generator reads files as data. It imports exactly two repository authorities that resolve names
at runtime -- the node contract registry and the declared workflow fixtures -- because a contract
reachable only through a registry cannot be found by a literal search, and V1 section 2 requires
those dynamic rules to be part of discovery. It opens no network, executes no scanned module,
follows no symlink or reparse component, and writes exactly one artifact.

Two rules run through everything. The absence of a reference is never evidence that a contract is
unused: an entry whose readers cannot be resolved fails closed as ``NEED_EVIDENCE`` and can make no
retirement claim. And a boundary this repository does not control -- public, persisted,
cross-language, untrusted-input or host-core -- is never a direct retirement candidate, whatever an
override says.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.contract_inventory import (  # noqa: E402
    CompatibilityDisposition,
    ContractBoundary,
    ContractDisposition,
    ContractEntry,
    ContractInventory,
    ContractInventoryError,
    ContractKind,
    InventoryBlocker,
    PersistenceClass,
    PublicationStatus,
    build_contract_inventory,
)
from comfyui_h3_context.core.reachability import (  # noqa: E402
    ReachabilityDisposition,
    ReachabilitySource,
)

ARTIFACT_PATH = Path("comfyui_h3_context/contracts/contract_inventory_v1.json")
#: The inventory's own artifact and its own test describe the inventory; they name contract
#: identities in order to assert *about* the surface, not because they consume those contracts.
#: Indexing them would let the inventory manufacture its own evidence -- a test asserting that a
#: contract has no reader would become that contract's reader -- so both are excluded and the
#: surface stays a measurement of the repository rather than of itself.
#:
#: The M18-02 fingerprint-domain record is excluded for exactly that reason and no other: it names
#: every identity in this inventory in order to assign each one a domain.  Indexed, it would become
#: the sole discovered reader of every contract nothing else reads, silently clearing the
#: `unknown_consumer` blockers that are this inventory's only honest answer for them.  The
#: *generator* stays indexed -- it hard-codes eight identities in its declaration table and is a
#: reader of those eight in the ordinary sense.
SELF_DESCRIBING_PATHS = frozenset(
    {
        ARTIFACT_PATH.as_posix(),
        "tests/test_contract_inventory.py",
        "governance/contracts/fingerprint_domain_v1.json",
        # M18-05's eligibility record names every inventoried schema, so leaving it indexed
        # would give every one of them one more apparent reader -- and the reader would be
        # the record deciding whether they have readers.  Third artifact to need this
        # exclusion; the shape is always the same: a record *about* the contracts is not a
        # *consumer* of them.
        "governance/contracts/retirement_eligibility_v1.json",
        # IMPORTANT: these files describe change-impact ownership; they do not read the named
        # artifacts at runtime. Indexing them would manufacture consumers for every contract the
        # fitness map is specifically trying to keep independently owned.
        "governance/contracts/architecture_fitness_v1.json",
        "scripts/architecture_fitness.py",
        "tests/test_architecture_fitness.py",
        # Fourth instance, from M23-48, and the broadest: the readership record lists *every*
        # packaged artifact by path, so indexing it would hand a fresh consumer to all of them at
        # once -- including the schemas whose only question is whether anyone consumes them. Its
        # test pins the two retained classes by path for the same reason, and is excluded on the
        # same grounds: naming a contract in order to state who reads it is not reading it.
        "governance/contracts/packaged_artifact_readership_v1.json",
        "tests/test_m23_48_packaged_readership.py",
    }
)
#: Both contracts roots. M23-55 moved every artifact with no runtime or bundle reader out
#: of the installed package, and the inventory indexes repository authorities rather than
#: shipped files, so it scans both. Rows already carry full paths, so the record's shape is
#: unchanged; dropping the second root would silently un-inventory 134 contracts.
CONTRACT_ROOTS = (
    Path("comfyui_h3_context/contracts"),
    Path("governance/contracts"),
)

#: Frozen scan roots (V2 section 4). Anything outside them is not a repository authority: in
#: particular `reference/` is read-only third-party material and `.planning/` is ignored governance.
SCAN_ROOTS = (
    Path("comfyui_h3_context"),
    Path("governance"),
    Path("frontend/src"),
    Path("frontend/tests"),
    Path("scripts"),
    Path("tests"),
    Path("workflows"),
    Path("subgraphs"),
)
ROOT_FILES = (Path("pyproject.toml"), Path("MANIFEST.in"), Path(".comfyignore"))
EXCLUDED_DIRECTORIES = frozenset(
    {"__pycache__", "node_modules", "dist", "build", ".tmp", "test-results", "test-results-host"}
)
#: CRITICAL: Markdown is deliberately absent.  The scan indexes path-like tokens, so a prose
#: document that *mentions* a contract path -- which every SOP and matrix does, because that is
#: what describing a process looks like -- was recorded as a consumer of it, and the artifact
#: then went stale on a pure prose edit.  AGENTS.md section 5.1 forbids exactly that: a prose
#: document must stay rewritable without a test turning red, and `--check` inside the gate is
#: such a test.  This is the same shape `SELF_DESCRIBING_PATHS` exists for -- a record *about*
#: the contracts is not a *consumer* of them -- but stated once by construction rather than as a
#: list somebody has to remember to extend for the next document.  Re-adding ".md" here
#: reintroduces the defect for every Markdown file in the scan roots at once.
SCANNED_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".json", ".toml", ".css", ".in", ""})
MAX_SCANNED_FILES = 4_096
MAX_SCANNED_BYTES = 48 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_SCHEMA_CONSTANT = re.compile(r"^([A-Z][A-Z0-9_]*_SCHEMA)\s*=\s*\"([^\"]+)\"", re.M)
#: One pass over each file yields every run that could name a contract; the runs are then split so a
#: module referenced as `../contracts/sidebarWorkspaceCodec` still matches by its own name.
#: A leading dot is admitted so a dotfile authority such as `.comfyignore` is discoverable at
#: all; without it the entry would report "no reader" for a file every packaging rule reads.
_TOKEN = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.:/@+-]{2,199}")

#: Surfaces that parse host, provider, model, media or user-supplied data. Assigning this boundary
#: only ever restricts what may be claimed, so a generous list is the safe direction to be wrong in.
_UNTRUSTED_MARKERS = (
    "media",
    "workflow_migration",
    "vlm",
    "ocr",
    "asr",
    "audio",
    "video",
    "model_manifest",
    "provider",
    "compatibility",
    "host",
    "transfer",
    "registry_payload",
    "supply_chain",
    "sbom",
    "official_oracle",
    "corpus",
)
#: Surfaces whose values outlive the process, so they have readers a later change cannot recall.
_PERSISTED_MARKERS = (
    "receipt",
    "checkpoint",
    "ledger",
    "artifact",
    "supply_chain",
    "sbom",
    "manifest",
    "profiles",
    "matrix",
    "baseline",
)
_PUBLIC_ENTRY_MODULES = (
    "comfyui_h3_context/nodes.py",
    "comfyui_h3_context/product_shell_node.py",
    "comfyui_h3_context/semantic_proposal_node.py",
    "comfyui_h3_context/public_api.py",
    "comfyui_h3_context/registration.py",
)


@dataclass(frozen=True)
class _Override:
    """A declared decision the mechanical rules cannot make, and the reason it was made."""

    evidence: str
    canonical_authority: str | None = None
    boundary: ContractBoundary | None = None
    disposition: ContractDisposition | None = None
    deferred_consumer_note: str | None = None


#: Bounded and explicit. An override may add a reason or resolve an ownership question; it may never
#: promote an entry past a rule the validator enforces.
OVERRIDES: Mapping[str, _Override] = {
    "h3.context.generation_profile.v1": _Override(
        evidence=(
            "one authority in core, re-declared by its own adapter; ownership is declared, "
            "not inferred from the duplicate declaration"
        ),
        canonical_authority="comfyui_h3_context/core/generation_profile.py",
    ),
    "h3-context-subgraph-fixture/1": _Override(
        evidence=(
            "two structurally different legacy subgraph fixture families share one identifier; "
            "ownership is not inferred and the identity is recorded as a blocker"
        ),
    ),
}


class InventoryGenerationError(RuntimeError):
    """Raised when discovery cannot proceed safely or within its declared bounds."""


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _iter_files() -> Iterable[Path]:
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        if _is_link_or_reparse(base):
            raise InventoryGenerationError(f"scan root {root.as_posix()} is a link")
        stack = [base]
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir()):
                if _is_link_or_reparse(child):
                    continue
                if child.is_dir():
                    if child.name not in EXCLUDED_DIRECTORIES:
                        stack.append(child)
                elif (
                    child.is_file()
                    and child.suffix in SCANNED_SUFFIXES
                    and _relative(child) not in SELF_DESCRIBING_PATHS
                ):
                    yield child
    for name in ROOT_FILES:
        candidate = ROOT / name
        if candidate.is_file() and not _is_link_or_reparse(candidate):
            yield candidate


def _tokens(text: str) -> set[str]:
    found: set[str] = set()
    for run in _TOKEN.findall(text):
        found.add(run)
        if "/" in run:
            found.update(part for part in run.split("/") if part)
    return found


def scan_repository() -> dict[str, set[str]]:
    """Read every scanned file once and index the runs that could name a contract."""

    index: dict[str, set[str]] = {}
    total_bytes = 0
    for path in _iter_files():
        if len(index) >= MAX_SCANNED_FILES:
            raise InventoryGenerationError("scan exceeded the declared file budget")
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            # A generated bundle is not an authority and is not a reader; skipping it is recorded
            # here rather than silently, because a skipped file cannot supply evidence.
            continue
        total_bytes += len(raw)
        if total_bytes > MAX_SCANNED_BYTES:
            raise InventoryGenerationError("scan exceeded the declared byte budget")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        index[_relative(path)] = _tokens(text)
    if not index:
        raise InventoryGenerationError("scan found no files")
    return index


@dataclass(frozen=True)
class _Candidate:
    contract_id: str
    kind: ContractKind
    authority_paths: tuple[str, ...]
    dialect: str
    needles: frozenset[str]
    fingerprint_domain: str = "none"
    forced_boundary: ContractBoundary | None = None
    blockers: tuple[InventoryBlocker, ...] = ()


def _json_schema_candidates() -> list[_Candidate]:
    found: list[_Candidate] = []
    candidates = sorted(
        path for root in CONTRACT_ROOTS for path in (ROOT / root).glob("*.schema.json")
    )
    for path in candidates:
        document = json.loads(path.read_text(encoding="utf-8"))
        raw_identity = document.get("$id")
        # JSON Schema Core makes `$id` the resource identity. A file without one has no identity to
        # own, so the path stands in and the missing owner is recorded rather than invented.
        resolved = isinstance(raw_identity, str) and bool(raw_identity)
        identity = raw_identity if resolved else _relative(path)
        dialect = document.get("$schema")
        found.append(
            _Candidate(
                contract_id=identity,
                kind=ContractKind.JSON_SCHEMA,
                authority_paths=(_relative(path),),
                dialect=dialect if isinstance(dialect, str) and dialect else "unstated",
                needles=frozenset({identity, path.name, path.name.removesuffix(".schema.json")}),
                blockers=() if resolved else (InventoryBlocker.UNRESOLVED_OWNER,),
            )
        )
    return found


def _json_data_candidates() -> list[_Candidate]:
    found: list[_Candidate] = []
    candidates = sorted(path for root in CONTRACT_ROOTS for path in (ROOT / root).glob("*.json"))
    for path in candidates:
        # The inventory is not a contract it inventories.  Reading its own bytes would also make
        # every entry appear to have the artifact as a reader, and generation would never reach a
        # fixed point.  Its identity is already covered by the Python wire that declares it.
        if path.name.endswith(".schema.json") or _relative(path) in SELF_DESCRIBING_PATHS:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        declared = document.get("schema") if isinstance(document, dict) else None
        identity = declared if isinstance(declared, str) and declared else path.name
        found.append(
            _Candidate(
                contract_id=identity,
                kind=ContractKind.JSON_DATA,
                authority_paths=(_relative(path),),
                dialect="not_applicable",
                needles=frozenset({identity, path.name, path.stem}),
            )
        )
    return found


def _python_wire_candidates() -> list[_Candidate]:
    declarations: dict[str, dict[str, set[str]]] = {}
    for path in sorted((ROOT / "comfyui_h3_context").rglob("*.py")):
        if "__pycache__" in path.parts or _is_link_or_reparse(path):
            continue
        for name, value in _SCHEMA_CONSTANT.findall(path.read_text(encoding="utf-8")):
            record = declarations.setdefault(value, {"paths": set(), "names": set()})
            record["paths"].add(_relative(path))
            record["names"].add(name)
    found: list[_Candidate] = []
    for value, record in declarations.items():
        found.append(
            _Candidate(
                contract_id=value,
                kind=ContractKind.PYTHON_WIRE,
                authority_paths=tuple(sorted(record["paths"])),
                dialect="not_applicable",
                needles=frozenset({value, *record["names"]}),
                fingerprint_domain=value,
            )
        )
    return found


def _frontend_codec_candidates() -> list[_Candidate]:
    found: list[_Candidate] = []
    for path in sorted((ROOT / "frontend/src/contracts").glob("*.ts")):
        found.append(
            _Candidate(
                contract_id=f"frontend/{path.stem}",
                kind=ContractKind.FRONTEND_CODEC,
                authority_paths=(_relative(path),),
                dialect="not_applicable",
                needles=frozenset({path.stem, path.name}),
                forced_boundary=ContractBoundary.CROSS_LANGUAGE,
            )
        )
    return found


def _workflow_fixture_candidates() -> list[_Candidate]:
    from comfyui_h3_context.core.public_manifest import DEFAULT_WORKFLOW_FIXTURES

    found: list[_Candidate] = []
    for fixture in DEFAULT_WORKFLOW_FIXTURES:
        found.append(
            _Candidate(
                contract_id=fixture.fixture_id,
                kind=ContractKind.WORKFLOW_FIXTURE,
                authority_paths=(fixture.path,),
                dialect="not_applicable",
                needles=frozenset({fixture.fixture_id, Path(fixture.path).name}),
            )
        )
    return found


def _host_node_candidates() -> list[_Candidate]:
    from comfyui_h3_context.core import native_h3

    pinned = f"{native_h3.NATIVE_H3_SOURCE} @ {native_h3.NATIVE_H3_HOST_VERSION}"
    authority = ("comfyui_h3_context/core/native_h3.py",)
    found = [
        _Candidate(
            contract_id=node_id,
            kind=ContractKind.HOST_NODE,
            authority_paths=authority,
            dialect="not_applicable",
            needles=frozenset({node_id}),
            fingerprint_domain=pinned,
            forced_boundary=ContractBoundary.HOST_CORE,
        )
        for node_id in (native_h3.NATIVE_H3_IMAGE_NODE_ID, native_h3.NATIVE_H3_REFERENCE_NODE_ID)
    ]
    return found


def _package_metadata_candidates() -> list[_Candidate]:
    found: list[_Candidate] = []
    for name in ROOT_FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        found.append(
            _Candidate(
                contract_id=f"package/{name.as_posix()}",
                kind=ContractKind.PACKAGE_METADATA,
                authority_paths=(name.as_posix(),),
                dialect="not_applicable",
                needles=frozenset({name.as_posix(), name.name}),
                forced_boundary=ContractBoundary.PUBLIC,
            )
        )
    return found


def _registry_consumers() -> frozenset[str]:
    """The dynamic registries a literal search cannot follow (V2 section 4, rule 5)."""

    from comfyui_h3_context.core.node_contracts import default_node_contract_registry

    registry = default_node_contract_registry()
    if not registry.definitions:
        raise InventoryGenerationError("node contract registry resolved to nothing")
    return frozenset({"comfyui_h3_context/core/node_contracts.py", "comfyui_h3_context/nodes.py"})


def _references(
    candidate: _Candidate, index: Mapping[str, set[str]]
) -> tuple[list[str], list[str]]:
    """Split references the only way the evidence supports: who declares it, and who reads it.

    A path-prefix split -- "package code produces, tests consume" -- reads well and is wrong: a core
    module importing another core module's schema constant is a reader, not a second producer.  So
    the producers are exactly the authority sites, and every other file naming the identity under
    any discovery rule is a consumer.  An empty consumer list then means something real: nothing in
    the repository outside the declaring file names this contract at all.
    """

    authority = set(candidate.authority_paths)
    consumers = {
        path
        for path, tokens in index.items()
        if path not in authority and (candidate.needles & tokens)
    }
    return sorted(authority), sorted(consumers)


def _boundary(candidate: _Candidate, consumers: Sequence[str]) -> ContractBoundary:
    if candidate.forced_boundary is not None:
        return candidate.forced_boundary
    haystack = " ".join((candidate.contract_id, *candidate.authority_paths)).casefold()
    if any(marker in haystack for marker in _UNTRUSTED_MARKERS):
        return ContractBoundary.UNTRUSTED_INPUT
    if any(path.startswith("frontend/") for path in consumers):
        return ContractBoundary.CROSS_LANGUAGE
    if candidate.kind is ContractKind.JSON_DATA or any(
        marker in haystack for marker in _PERSISTED_MARKERS
    ):
        return ContractBoundary.PERSISTED
    if (
        candidate.kind is ContractKind.JSON_SCHEMA
        or candidate.kind is ContractKind.WORKFLOW_FIXTURE
    ):
        return ContractBoundary.PUBLIC
    if all(path.startswith(("tests/", "scripts/m")) for path in candidate.authority_paths):
        return ContractBoundary.TEST_ONLY
    return ContractBoundary.PROCESS_LOCAL


def _publication(candidate: _Candidate) -> PublicationStatus:
    if all(
        path.startswith("comfyui_h3_context/") or path.startswith("workflows/")
        for path in candidate.authority_paths
    ):
        return PublicationStatus.PACKAGED
    if candidate.kind is ContractKind.PACKAGE_METADATA:
        return PublicationStatus.PACKAGED
    return PublicationStatus.REPOSITORY_ONLY


def _persistence(candidate: _Candidate, boundary: ContractBoundary) -> PersistenceClass:
    if boundary is ContractBoundary.PERSISTED or candidate.kind in {
        ContractKind.JSON_DATA,
        ContractKind.PACKAGE_METADATA,
        ContractKind.WORKFLOW_FIXTURE,
    }:
        return PersistenceClass.PERSISTED
    return PersistenceClass.TRANSIENT


def _on_the_comfyui_boundary(path: str) -> bool:
    return (
        path.startswith("comfyui_h3_context/adapters/")
        or path.endswith("_node.py")
        or path == "comfyui_h3_context/nodes.py"
    )


def _carries_a_route(candidate: _Candidate, referencing: Iterable[str]) -> bool:
    """Whether the reachability question is even about this contract.

    `core/reachability.py` asks whether a normal public workflow can reach a field or a task mode
    through a named producer. That question is about routes: host node types, workflow fixtures,
    browser codecs, and anything an adapter or node module touches on the ComfyUI boundary. It is
    not about an internal core wire, and answering `unsupported` for one would read as a product
    claim about a contract nobody ever offered.

    Both sides count. The VLM observation wire is declared in `core/` and only reaches ComfyUI
    through `adapters/comfyui_vlm.py`; looking at authority sites alone would drop the claim for
    precisely the route whose unreachability M21-02 established and this inventory must preserve.
    """

    if candidate.kind in {
        ContractKind.HOST_NODE,
        ContractKind.WORKFLOW_FIXTURE,
        ContractKind.FRONTEND_CODEC,
    }:
        return True
    return any(_on_the_comfyui_boundary(path) for path in referencing)


def _reachability(
    candidate: _Candidate,
    producers: Sequence[str],
    consumers: Sequence[str],
    registry_entries: frozenset[str],
) -> tuple[ReachabilityDisposition | None, ReachabilitySource | None]:
    """Map discovered references onto the vocabulary `core/reachability.py` already owns.

    `reachable`/`public_node` means a registered node module names the identity, so an ordinary
    workflow can reach it. `reachable`/`host_input` means the browser host layer names it.
    `injected_only` means only tests and fixtures do, which is evidence about a test, never about a
    runtime. `unsupported` is reserved for a contract that *is* a route and that no public surface
    reaches -- the accepted state of the VLM observation route. Anything else records no claim.
    """

    referencing = set(producers) | set(consumers)
    if referencing & (set(_PUBLIC_ENTRY_MODULES) | registry_entries):
        return ReachabilityDisposition.REACHABLE, ReachabilitySource.PUBLIC_NODE
    if any(path.startswith("frontend/src/host/") for path in referencing):
        return ReachabilityDisposition.REACHABLE, ReachabilitySource.HOST_INPUT
    if referencing and all(
        path.startswith(("tests/", "frontend/tests/", "scripts/m")) for path in referencing
    ):
        return ReachabilityDisposition.INJECTED_ONLY, ReachabilitySource.INJECTED_FIXTURE
    if _carries_a_route(candidate, referencing | set(candidate.authority_paths)):
        return ReachabilityDisposition.UNSUPPORTED, ReachabilitySource.NONE
    return None, None


def _entry(
    candidate: _Candidate, index: Mapping[str, set[str]], registry_entries: frozenset[str]
) -> ContractEntry:
    producers, consumers = _references(candidate, index)
    boundary = _boundary(candidate, consumers)
    override = OVERRIDES.get(candidate.contract_id)
    if override is not None and override.boundary is not None:
        boundary = override.boundary

    blockers: list[InventoryBlocker] = list(candidate.blockers)
    if len(candidate.authority_paths) > 1 and (
        override is None or override.canonical_authority is None
    ):
        blockers.append(InventoryBlocker.DUPLICATE_SCHEMA_IDENTITY)
    if not consumers:
        blockers.append(InventoryBlocker.UNKNOWN_CONSUMER)

    if blockers:
        disposition = ContractDisposition.NEED_EVIDENCE
        if override is not None:
            evidence = override.evidence
        elif InventoryBlocker.UNRESOLVED_OWNER in blockers:
            evidence = "the authority declares no $id, so no identity can be attributed to it"
        elif InventoryBlocker.DUPLICATE_SCHEMA_IDENTITY in blockers:
            evidence = (
                "one identity is declared by more than one authority; ownership is not inferred"
            )
        else:
            evidence = "no reader resolved by the five discovery rules; absence is not disuse"
    elif override is not None and override.disposition is not None:
        disposition = override.disposition
        evidence = override.evidence
    elif boundary in {
        ContractBoundary.PUBLIC,
        ContractBoundary.PERSISTED,
        ContractBoundary.CROSS_LANGUAGE,
        ContractBoundary.UNTRUSTED_INPUT,
    }:
        disposition = ContractDisposition.KEEP
        evidence = f"{boundary.value} boundary keeps a reader this repository cannot re-migrate"
    elif boundary is ContractBoundary.HOST_CORE:
        disposition = ContractDisposition.KEEP
        evidence = "host contract pinned by this repository and owned by the host"
    else:
        disposition = ContractDisposition.KEEP
        evidence = (
            override.evidence
            if override is not None
            else "readers resolved inside this repository; no simplification evidence gathered"
        )

    compatibility = (
        CompatibilityDisposition.MIGRATION_REQUIRED
        if boundary in {ContractBoundary.PERSISTED, ContractBoundary.CROSS_LANGUAGE}
        else CompatibilityDisposition.OLD_READERS_UNAFFECTED
        if consumers
        else CompatibilityDisposition.UNKNOWN
    )
    reachability, source = _reachability(candidate, producers, consumers, registry_entries)
    return ContractEntry(
        contract_id=candidate.contract_id,
        kind=candidate.kind,
        authority_paths=tuple(sorted(candidate.authority_paths)),
        dialect=candidate.dialect,
        producers=tuple(producers),
        consumers=tuple(consumers),
        boundary=boundary,
        persistence=_persistence(candidate, boundary),
        publication=_publication(candidate),
        reachability=reachability,
        reachability_source=source,
        fingerprint_domain=candidate.fingerprint_domain,
        disposition=disposition,
        compatibility=compatibility,
        evidence=evidence,
        blockers=tuple(sorted(blockers, key=lambda item: item.value)),
        deferred_consumer_note=None if override is None else override.deferred_consumer_note,
    )


def build_inventory() -> ContractInventory:
    """Discover every contract authority once and classify it deterministically."""

    index = scan_repository()
    registry_entries = _registry_consumers()
    candidates = (
        _json_schema_candidates()
        + _json_data_candidates()
        + _python_wire_candidates()
        + _frontend_codec_candidates()
        + _workflow_fixture_candidates()
        + _host_node_candidates()
        + _package_metadata_candidates()
    )
    seen: set[tuple[str, str]] = set()
    entries: list[ContractEntry] = []
    for candidate in candidates:
        key = (candidate.kind.value, candidate.contract_id)
        if key in seen:
            raise InventoryGenerationError(f"contract {candidate.contract_id!r} discovered twice")
        seen.add(key)
        entries.append(_entry(candidate, index, registry_entries))
    unknown = sorted(set(OVERRIDES) - {item.contract_id for item in entries})
    if unknown:
        raise InventoryGenerationError(f"override names an unknown contract: {unknown[0]}")
    return build_contract_inventory(tuple(entries))


def artifact_bytes(inventory: ContractInventory) -> bytes:
    return (
        json.dumps(inventory.to_wire(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        inventory = build_inventory()
    except (ContractInventoryError, InventoryGenerationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(inventory)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check:
        current = target.read_bytes() if target.is_file() else b""
        if current != expected:
            print(
                json.dumps(
                    {"status": "FAIL", "detail": "contract inventory artifact is stale"},
                    ensure_ascii=False,
                )
            )
            return 1
    print(json.dumps({"status": "PASS", **inventory.to_public_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
