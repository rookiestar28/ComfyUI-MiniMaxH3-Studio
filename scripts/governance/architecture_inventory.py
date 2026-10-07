"""What this package is made of, which seams can move, and which are pinned by something.

M19-01 is the baseline the rest of the M19 chain is measured against.  Its job is not to describe
the architecture -- prose already does that -- but to make three questions answerable mechanically,
because each one has an obvious wrong answer that a scan will happily produce.

**Which layer does a module belong to?**  Declared, never inferred from a path prefix.  Inferring it
turned `nodes.py` into a layer of its own and reported two "inversions" that were lateral edges
between sibling node-UI modules.  A dependency-direction guard is only as good as its layer
assignment, so the assignment ships as data.

**Does an import edge count?**  Counting every ``ImportFrom`` finds two cyclic components and
cries wolf about two deliberate constructions.  Ignoring ``TYPE_CHECKING`` and function-local
imports finds none and hides the coupling entirely.  Both answers are defensible and neither is a
baseline, so edges carry their kind and cycles record what they close through.  The regression
worth catching is a new **module-level** back-edge -- the kind that actually breaks an import --
and that is the one this record refuses outright.

**Which test owns a module?**  Scanning for a direct import of the module by name resolves 89 of 154
and reports `core.semantic_graph_comparator` as untested while a 600-line suite exercises it through
``importlib.import_module``.  Four rules are needed, and the fourth exists only because
``core/__init__.py`` re-exports two thousand names: for a third of the package the sole way to know
which test covers which module is to attribute a hub symbol back to where it is defined.  Symbols
defined by two modules attribute to neither.

Whatever survives all four rules unresolved is ``NEED_EVIDENCE`` and blocks a move.  It is never
"unused": M18-01's rule holds that an undiscovered consumer is not an absent one.

This module reads no file, parses no source and imports no host.  It is the typed shape the
generator fills.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.errors import ContractValidationError

ARCHITECTURE_INVENTORY_SCHEMA = "h3-context-architecture-inventory/1"

MAX_MODULES = 512
MAX_EDGES_PER_MODULE = 256
MAX_COMPONENT_MEMBERS = 64
MAX_TEXT = 200
MAX_PATH_LENGTH = 260
MAX_COHORT_MODULES = 32

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.:/@+-]{0,199}\Z")


class ArchitectureInventoryError(ContractValidationError):
    """Raised when an architecture baseline would record something it cannot stand behind."""


class Layer(str, Enum):
    """The three layers, ordered.  A module imports its own layer or a lower one, never higher."""

    CORE = "core"
    ADAPTERS = "adapters"
    NODE_UI = "node_ui"


#: Lower rank may not import higher rank.  `node_ui` is the package root, `nodes.py` included --
#: it is not a layer of its own, and treating it as one manufactures false inversions.
LAYER_RANK: dict[Layer, int] = {Layer.CORE: 0, Layer.ADAPTERS: 1, Layer.NODE_UI: 2}


class EdgeKind(str, Enum):
    """How an import reaches its target, which decides whether it can close a runtime cycle."""

    MODULE_LEVEL = "module_level"
    FUNCTION_LOCAL = "function_local"
    TYPE_CHECKING = "type_checking"


#: The kinds that do not execute at import time.  A cycle closing only through these is not a
#: runtime cycle: deferring the import is the standard way of breaking one.
DEFERRED_KINDS = frozenset({EdgeKind.FUNCTION_LOCAL, EdgeKind.TYPE_CHECKING})


class OwnerRule(str, Enum):
    """How a test was found to exercise a module.  Each rule recovers what the previous misses."""

    DIRECT_MODULE = "direct_module"
    SUBMODULE_AS_NAME = "submodule_as_name"
    IMPORTLIB_LITERAL = "importlib_literal"
    HUB_SYMBOL = "hub_symbol"
    QUALIFICATION_SUBJECT = "qualification_subject"


class ModuleDisposition(str, Enum):
    """Whether a cohort may propose moving this module."""

    MOVABLE = "movable"
    NEED_EVIDENCE = "need_evidence"


def _text(value: str, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureInventoryError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ArchitectureInventoryError(f"{field_name} exceeds {maximum} characters")
    return value


def _module_name(value: str, field_name: str) -> str:
    _text(value, field_name)
    if not _MODULE.fullmatch(value):
        raise ArchitectureInventoryError(f"{field_name} is not a dotted module name")
    return value


def _relative_path(value: str, field_name: str) -> str:
    _text(value, field_name, maximum=MAX_PATH_LENGTH)
    if value.startswith("/") or ":" in value or ".." in value.split("/"):
        raise ArchitectureInventoryError(f"{field_name} must be a repository-relative path")
    if not _RELATIVE_PATH.fullmatch(value):
        raise ArchitectureInventoryError(f"{field_name} is not a relative path")
    return value


def _identifier(value: str, field_name: str) -> str:
    _text(value, field_name)
    if not _IDENTIFIER.fullmatch(value):
        raise ArchitectureInventoryError(f"{field_name} is not a stable identifier")
    return value


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    ordered = tuple(values)
    if list(ordered) != sorted(ordered) or len(set(ordered)) != len(ordered):
        raise ArchitectureInventoryError(f"{field_name} must be sorted and unique")
    return ordered


@dataclass(frozen=True, slots=True)
class ImportEdge:
    """One directed import between two modules in this package, with every kind that carries it.

    The same pair can be carried by more than one kind at once: `core.contracts` annotates with
    `EvidenceSet` under ``TYPE_CHECKING`` and constructs it inside a function.  Both are recorded,
    because dropping either would misreport why the edge exists.
    """

    source: str
    target: str
    kinds: tuple[EdgeKind, ...]

    def __post_init__(self) -> None:
        _module_name(self.source, "source")
        _module_name(self.target, "target")
        if self.source == self.target:
            raise ArchitectureInventoryError("an import edge may not be a self-loop")
        if not self.kinds:
            raise ArchitectureInventoryError("an import edge must record at least one kind")
        values = tuple(kind.value for kind in self.kinds)
        _sorted_unique(values, "kinds")

    @property
    def deferred(self) -> bool:
        """True when nothing about this edge executes at import time."""

        return all(kind in DEFERRED_KINDS for kind in self.kinds)

    def to_wire(self) -> dict[str, object]:
        return {
            "kinds": [kind.value for kind in self.kinds],
            "source": self.source,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class ModuleRow:
    """One module, its layer, its coupling, and whether anything is known to test it."""

    module: str
    layer: Layer
    lines: int
    inbound: int
    outbound: int
    owning_tests: tuple[str, ...] = ()
    owning_rules: tuple[OwnerRule, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _module_name(self.module, "module")
        counters = (("lines", self.lines), ("inbound", self.inbound), ("outbound", self.outbound))
        for label, value in counters:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ArchitectureInventoryError(f"{label} must be a non-negative integer")
        if self.inbound > MAX_EDGES_PER_MODULE or self.outbound > MAX_EDGES_PER_MODULE:
            raise ArchitectureInventoryError(f"edge count exceeds {MAX_EDGES_PER_MODULE}")
        for path in self.owning_tests:
            _relative_path(path, "owning_tests")
        _sorted_unique(self.owning_tests, "owning_tests")
        _sorted_unique(tuple(rule.value for rule in self.owning_rules), "owning_rules")
        # A test without the rule that found it, or a rule that found nothing, is a bookkeeping
        # error that would make the residual impossible to audit.
        if bool(self.owning_tests) != bool(self.owning_rules):
            raise ArchitectureInventoryError("owning tests and owning rules must agree")
        for reason in self.reasons:
            _text(reason, "reasons")

    @property
    def disposition(self) -> ModuleDisposition:
        """Fail closed: no discovered owner means no cohort may move it."""

        if self.owning_tests:
            return ModuleDisposition.MOVABLE
        return ModuleDisposition.NEED_EVIDENCE

    def to_wire(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "inbound": self.inbound,
            "layer": self.layer.value,
            "lines": self.lines,
            "module": self.module,
            "outbound": self.outbound,
            "owning_rules": [rule.value for rule in self.owning_rules],
            "owning_tests": list(self.owning_tests),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CyclicComponent:
    """A set of modules that reach each other, and the edge kinds that close the loop.

    ``closes_through`` is the whole point.  A component that closes through ``module_level`` is a
    real import cycle; one that closes only through deferred kinds is somebody having already broken
    a cycle by hand, which is a seam rather than a defect.
    """

    members: tuple[str, ...]
    closes_through: tuple[EdgeKind, ...]

    def __post_init__(self) -> None:
        if len(self.members) < 2:
            raise ArchitectureInventoryError("a cyclic component needs at least two members")
        if len(self.members) > MAX_COMPONENT_MEMBERS:
            raise ArchitectureInventoryError(f"component exceeds {MAX_COMPONENT_MEMBERS} members")
        for member in self.members:
            _module_name(member, "members")
        _sorted_unique(self.members, "members")
        if not self.closes_through:
            raise ArchitectureInventoryError("a cyclic component must say what closes it")
        _sorted_unique(tuple(kind.value for kind in self.closes_through), "closes_through")

    @property
    def runtime_cycle(self) -> bool:
        return EdgeKind.MODULE_LEVEL in self.closes_through

    def to_wire(self) -> dict[str, object]:
        return {
            "closes_through": [kind.value for kind in self.closes_through],
            "members": list(self.members),
            "runtime_cycle": self.runtime_cycle,
        }


@dataclass(frozen=True, slots=True)
class SurfaceOverlap:
    """How much of one declared public surface appears in another."""

    other: str
    shared: int

    def __post_init__(self) -> None:
        _identifier(self.other, "other")
        if not isinstance(self.shared, int) or isinstance(self.shared, bool) or self.shared < 0:
            raise ArchitectureInventoryError("shared must be a non-negative integer")

    def to_wire(self) -> dict[str, object]:
        return {"other": self.other, "shared": self.shared}


@dataclass(frozen=True, slots=True)
class PublicSurface:
    """One authority that claims to say what this package exposes.

    There is more than one, and they disagree.  Recording each with its overlaps makes the
    disagreement a row rather than a discovery someone makes halfway through an export reduction.
    """

    surface_id: str
    authority: str
    declared: int
    overlaps: tuple[SurfaceOverlap, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.surface_id, "surface_id")
        _relative_path(self.authority, "authority")
        if (
            not isinstance(self.declared, int)
            or isinstance(self.declared, bool)
            or self.declared < 0
        ):
            raise ArchitectureInventoryError("declared must be a non-negative integer")
        others = tuple(item.other for item in self.overlaps)
        _sorted_unique(others, "overlaps")
        if self.surface_id in others:
            raise ArchitectureInventoryError("a surface may not overlap itself")
        for item in self.overlaps:
            if item.shared > self.declared:
                raise ArchitectureInventoryError("overlap exceeds the declared surface")

    def to_wire(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "declared": self.declared,
            "overlaps": [item.to_wire() for item in self.overlaps],
            "surface_id": self.surface_id,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceBinding:
    """A `tests/acceptance_baseline.json` row that a cohort would have to re-record if it moved.

    These fail at the Full Gate rather than at review, which is why they are enumerated here with
    the successor that owns each one instead of being rediscovered by a failing gate.
    """

    binding_id: str
    kind: str
    path: str
    owner: str

    def __post_init__(self) -> None:
        _identifier(self.binding_id, "binding_id")
        if self.kind not in {"public_python_abi", "criterion"}:
            raise ArchitectureInventoryError("kind must be public_python_abi or criterion")
        _relative_path(self.path, "path")
        _identifier(self.owner, "owner")

    def to_wire(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "kind": self.kind,
            "owner": self.owner,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class Cohort:
    """A frozen successor scope: which modules it may touch and why."""

    cohort_id: str
    successor: str
    modules: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _identifier(self.cohort_id, "cohort_id")
        _identifier(self.successor, "successor")
        if not self.modules:
            raise ArchitectureInventoryError("a cohort must name at least one module")
        if len(self.modules) > MAX_COHORT_MODULES:
            raise ArchitectureInventoryError(f"cohort exceeds {MAX_COHORT_MODULES} modules")
        for module in self.modules:
            _module_name(module, "modules")
        _sorted_unique(self.modules, "modules")
        _text(self.reason, "reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "cohort_id": self.cohort_id,
            "modules": list(self.modules),
            "reason": self.reason,
            "successor": self.successor,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureInventory:
    """The baseline.  Four things it refuses to record, because each is the regression to catch."""

    modules: tuple[ModuleRow, ...]
    deferred_edges: tuple[ImportEdge, ...] = ()
    cycles: tuple[CyclicComponent, ...] = ()
    surfaces: tuple[PublicSurface, ...] = ()
    bindings: tuple[AcceptanceBinding, ...] = ()
    cohorts: tuple[Cohort, ...] = ()
    forbidden_imports: tuple[str, ...] = ()
    layer_inversions: tuple[str, ...] = ()
    schema: str = ARCHITECTURE_INVENTORY_SCHEMA
    edge_kind_totals: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != ARCHITECTURE_INVENTORY_SCHEMA:
            raise ArchitectureInventoryError("unsupported architecture inventory schema")
        if not self.modules:
            raise ArchitectureInventoryError("an inventory must describe at least one module")
        if len(self.modules) > MAX_MODULES:
            raise ArchitectureInventoryError(f"inventory exceeds {MAX_MODULES} modules")
        names = tuple(row.module for row in self.modules)
        _sorted_unique(names, "modules")
        known = set(names)

        # 1. The pure core may not depend on a host, a runtime or a network stack.  Recorded as a
        #    refusal rather than a number, because a baseline that can hold a violation invites one.
        if self.forbidden_imports:
            raise ArchitectureInventoryError(
                f"pure core imports a forbidden dependency: {self.forbidden_imports[0]}"
            )

        # 2. Same for a layer inversion, under the declared layer model.
        if self.layer_inversions:
            raise ArchitectureInventoryError(
                f"a layer imports one above it: {self.layer_inversions[0]}"
            )

        for edge in self.deferred_edges:
            if not edge.deferred:
                raise ArchitectureInventoryError(
                    f"{edge.source} -> {edge.target} executes at import time and is not deferred"
                )
            for endpoint in (edge.source, edge.target):
                if endpoint not in known:
                    raise ArchitectureInventoryError(f"{endpoint} is not an inventoried module")
        _sorted_unique(
            tuple(f"{edge.source} {edge.target}" for edge in self.deferred_edges), "deferred_edges"
        )

        # 3. A cycle that closes at import time is a real cycle.  The deferred ones are recorded.
        for component in self.cycles:
            if component.runtime_cycle:
                raise ArchitectureInventoryError(
                    f"module-level import cycle: {', '.join(component.members)}"
                )
            for member in component.members:
                if member not in known:
                    raise ArchitectureInventoryError(f"{member} is not an inventoried module")
        _sorted_unique(tuple(c.members[0] for c in self.cycles), "cycles")

        _sorted_unique(tuple(item.surface_id for item in self.surfaces), "surfaces")
        _sorted_unique(tuple(item.binding_id for item in self.bindings), "bindings")
        _sorted_unique(tuple(item.cohort_id for item in self.cohorts), "cohorts")

        # 4. A cohort may not propose moving a module nothing is known to test.
        unresolved = {
            row.module for row in self.modules if row.disposition is ModuleDisposition.NEED_EVIDENCE
        }
        for cohort in self.cohorts:
            for module in cohort.modules:
                if module not in known:
                    raise ArchitectureInventoryError(f"{module} is not an inventoried module")
                if module in unresolved:
                    raise ArchitectureInventoryError(
                        f"cohort {cohort.cohort_id} would move {module}, which has no owning test"
                    )

        for key, value in self.edge_kind_totals.items():
            if key not in {kind.value for kind in EdgeKind}:
                raise ArchitectureInventoryError(f"unknown edge kind {key}")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ArchitectureInventoryError("edge kind totals must be non-negative integers")

    @property
    def need_evidence(self) -> tuple[ModuleRow, ...]:
        return tuple(
            row for row in self.modules if row.disposition is ModuleDisposition.NEED_EVIDENCE
        )

    @property
    def runtime_cycles(self) -> tuple[CyclicComponent, ...]:
        """Always empty while the record validates.  Present so the claim reads, not implied."""

        return tuple(component for component in self.cycles if component.runtime_cycle)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for group in (
            self.modules,
            self.deferred_edges,
            self.cycles,
            self.surfaces,
            self.bindings,
            self.cohorts,
        ):
            for item in group:
                digest.update(canonical_bytes(item.to_wire()))
                digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "bindings": [item.to_wire() for item in self.bindings],
            "cohorts": [item.to_wire() for item in self.cohorts],
            "cycles": [item.to_wire() for item in self.cycles],
            "deferred_edges": [item.to_wire() for item in self.deferred_edges],
            "edge_kind_totals": dict(sorted(self.edge_kind_totals.items())),
            "fingerprint": self.fingerprint,
            "forbidden_imports": list(self.forbidden_imports),
            "layer_inversions": list(self.layer_inversions),
            "modules": [item.to_wire() for item in self.modules],
            "schema": self.schema,
            "surfaces": [item.to_wire() for item in self.surfaces],
        }


__all__ = [
    "ARCHITECTURE_INVENTORY_SCHEMA",
    "DEFERRED_KINDS",
    "LAYER_RANK",
    "MAX_COHORT_MODULES",
    "MAX_COMPONENT_MEMBERS",
    "MAX_EDGES_PER_MODULE",
    "MAX_MODULES",
    "AcceptanceBinding",
    "ArchitectureInventory",
    "ArchitectureInventoryError",
    "Cohort",
    "CyclicComponent",
    "EdgeKind",
    "ImportEdge",
    "Layer",
    "ModuleDisposition",
    "ModuleRow",
    "OwnerRule",
    "PublicSurface",
    "SurfaceOverlap",
]
