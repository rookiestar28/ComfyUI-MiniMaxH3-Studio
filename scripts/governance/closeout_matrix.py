"""What the M18 contract chain actually changed, and whether its identity domains stayed separate.

Two things live here because they answer the two questions a closeout has to answer.

**Metrics** carry a baseline and a final, each read from an accepted checkpoint's own artifact.  A
metric that did not move says so; the chain's headline number -- shipped schema files -- did not
move at all, and a closeout record that could not express "unchanged" would have no way to report
that honestly.

**Blast-radius rows** measure the claim M18-02 made and never checked end to end: that a digest
computed for one question cannot silently answer another.  Each row is one mutation to one domain's
input and the set of shipped fingerprint functions that moved.

Rows are not all equally strong, and the record says so rather than letting a reader assume they
are.  The ``semantic`` and ``presentation`` rows carry the weight: both recompute real product code
over a really-different world.  The ``fixture`` and ``release_integrity`` rows digest world fields
nothing else reads, so their separation is close to arithmetic.  The ``wire_contract`` row changes a
declared shape rather than an instance, so its world is unchanged by construction and only the
probe's own declared input moves.  All five recompute; they differ in how much the recomputation
could have surprised anyone.

"Moves only its own domain" is the obvious rule and it is wrong, because some identities are
*supposed* to follow another domain: a stale-render identity that did not move when the thing it
renders changed would be useless.  So each probe declares which domains' material may legitimately
move it, and that declaration is the reviewable part -- widening it is a visible claim, not a silent
one.  Four invariants:

1. a ``semantic`` probe may declare no non-semantic dependency.  This is the load-bearing one:
   ``SEMANTIC`` is the only domain a runtime may consume as an execution, cache or receipt identity,
   so an execution identity that moves for a render, a fixture or a bundle change is computed over
   material that has nothing to do with the result;
2. a mutation in domain ``D`` moves only probes that declare ``D``.  Anything else is an identity
   answering a question it was not computed for;
3. a ``semantic`` mutation moves at least one ``semantic`` probe -- the direction a separation
   argument does not cover, because a cache identity that fails to move when the meaning changes
   authorises reuse of the wrong result;
4. every mutation moves something.  A probe that never moves is not watching anything, and a matrix
   of inert probes satisfies rules 1 and 2 trivially.

Rules 1 and 2 alone are satisfied by a matrix where nothing ever happens.  That is why all four are
enforced in the same place.

This module computes no fingerprint and reads no file.  The caller runs the real functions and
brings the observation here to be checked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.contract_inventory import FORBIDDEN_RECORD_TEXT
from comfyui_h3_context.core.errors import ContractValidationError
from comfyui_h3_context.core.fingerprint_domain import IdentityDomain

CLOSEOUT_MATRIX_SCHEMA = "h3-context-closeout-matrix/1"
MAX_MATRIX_ENTRIES = 128
MAX_PROBES = 64
MAX_TEXT = 200
MAX_COUNT = 100_000_000

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class CloseoutMatrixError(ContractValidationError):
    """A closeout claim is malformed, or is stronger than the observation behind it."""


def _name(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise CloseoutMatrixError(f"{field_name} must be a bounded lower-case identifier")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise CloseoutMatrixError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CloseoutMatrixError(f"{field_name} contains a control character")
    if FORBIDDEN_RECORD_TEXT.search(value):
        raise CloseoutMatrixError(f"{field_name} looks like a private path, URL or credential")
    return value


def _count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise CloseoutMatrixError(f"{field_name} must be a bounded non-negative count")
    return value


def _names(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_PROBES:
        raise CloseoutMatrixError(f"{field_name} must be a bounded tuple")
    if list(value) != sorted(set(value)):
        raise CloseoutMatrixError(f"{field_name} must be sorted and unique")
    for item in value:
        _name(item, field_name)
    return value


@dataclass(frozen=True, slots=True)
class Probe:
    """One shipped fingerprint function, and the question it is entitled to answer."""

    probe_id: str
    domain: IdentityDomain
    #: The function or record this probe computes, as a repository-recognisable name. Not a path,
    #: because a probe follows its function if the module moves.
    authority: str
    #: Domains whose material may legitimately move this identity, beyond its own. A stale-render
    #: identity declares `semantic` here and is right to; an execution identity declaring anything
    #: here is refused below, which is the rule that actually protects cache reuse.
    depends_on: tuple[IdentityDomain, ...] = ()

    def __post_init__(self) -> None:
        _name(self.probe_id, "probe_id")
        if not isinstance(self.domain, IdentityDomain):
            raise CloseoutMatrixError("domain must be an IdentityDomain")
        _text(self.authority, "authority")
        if not isinstance(self.depends_on, tuple):
            raise CloseoutMatrixError("depends_on must be a tuple")
        if not all(isinstance(item, IdentityDomain) for item in self.depends_on):
            raise CloseoutMatrixError("depends_on must contain IdentityDomain values")
        values = [item.value for item in self.depends_on]
        if values != sorted(set(values)):
            raise CloseoutMatrixError("depends_on must be sorted and unique")
        if self.domain in self.depends_on:
            raise CloseoutMatrixError("depends_on lists domains beyond the probe's own")
        # Invariant 1. Refused at construction rather than per row, because it is a property of the
        # identity itself: an execution, cache or receipt identity computed over anything but
        # semantic material is wrong before any mutation is applied to it.
        if self.domain is IdentityDomain.SEMANTIC and self.depends_on:
            raise CloseoutMatrixError(
                f"semantic probe {self.probe_id!r} claims a non-semantic dependency"
            )

    @property
    def inputs(self) -> frozenset[IdentityDomain]:
        return frozenset({self.domain, *self.depends_on})

    def to_wire(self) -> dict[str, object]:
        return {
            "probe_id": self.probe_id,
            "domain": self.domain.value,
            "authority": self.authority,
            "depends_on": [item.value for item in self.depends_on],
        }


@dataclass(frozen=True, slots=True)
class CloseoutMetric:
    """One measured quantity, with where each end of it came from.

    ``unit`` says what is being counted, because "89 to 89" and "308056 to 72778" are not the same
    kind of statement and a reader should not have to infer which one they are looking at.
    """

    metric_id: str
    unit: str
    baseline: int
    final: int
    baseline_commit: str
    final_commit: str
    evidence: str

    def __post_init__(self) -> None:
        _name(self.metric_id, "metric_id")
        _name(self.unit, "unit")
        _count(self.baseline, "baseline")
        _count(self.final, "final")
        for value, field_name in (
            (self.baseline_commit, "baseline_commit"),
            (self.final_commit, "final_commit"),
        ):
            if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
                raise CloseoutMatrixError(f"{field_name} must be a full commit object name")
        _text(self.evidence, "evidence")

    @property
    def moved(self) -> bool:
        return self.baseline != self.final

    def to_wire(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "unit": self.unit,
            "baseline": self.baseline,
            "final": self.final,
            "moved": self.moved,
            "baseline_commit": self.baseline_commit,
            "final_commit": self.final_commit,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class BlastRadiusRow:
    """One mutation, and every probe that moved because of it."""

    mutation_id: str
    domain: IdentityDomain
    moved: tuple[str, ...]
    unmoved: tuple[str, ...]
    evidence: str

    def __post_init__(self) -> None:
        _name(self.mutation_id, "mutation_id")
        if not isinstance(self.domain, IdentityDomain):
            raise CloseoutMatrixError("domain must be an IdentityDomain")
        _names(self.moved, "moved")
        _names(self.unmoved, "unmoved")
        if set(self.moved) & set(self.unmoved):
            raise CloseoutMatrixError("a probe cannot both move and not move")
        # Invariant 4, checked before the others because an empty observation makes them vacuous.
        if not self.moved:
            raise CloseoutMatrixError(
                f"mutation {self.mutation_id!r} moved nothing, so it observes nothing"
            )
        _text(self.evidence, "evidence")

    def to_wire(self) -> dict[str, object]:
        return {
            "mutation_id": self.mutation_id,
            "domain": self.domain.value,
            "moved": list(self.moved),
            "unmoved": list(self.unmoved),
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CloseoutMatrix:
    """Every metric and every blast-radius row, with the invariants enforced across them."""

    probes: tuple[Probe, ...]
    metrics: tuple[CloseoutMetric, ...]
    rows: tuple[BlastRadiusRow, ...]
    schema: str = CLOSEOUT_MATRIX_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CLOSEOUT_MATRIX_SCHEMA:
            raise CloseoutMatrixError("unsupported closeout matrix schema")
        for group, kind, field_name in (
            (self.probes, Probe, "probes"),
            (self.metrics, CloseoutMetric, "metrics"),
            (self.rows, BlastRadiusRow, "rows"),
        ):
            if not isinstance(group, tuple) or not group or len(group) > MAX_MATRIX_ENTRIES:
                raise CloseoutMatrixError(f"{field_name} must be a bounded non-empty tuple")
            if not all(isinstance(item, kind) for item in group):
                raise CloseoutMatrixError(f"{field_name} contains an invalid value")
        for values, field_name in (
            ([item.probe_id for item in self.probes], "probes"),
            ([item.metric_id for item in self.metrics], "metrics"),
            ([item.mutation_id for item in self.rows], "rows"),
        ):
            if values != sorted(values) or len(set(values)) != len(values):
                raise CloseoutMatrixError(f"{field_name} must be sorted and unique by identifier")

        by_id = {item.probe_id: item for item in self.probes}
        semantic = {item.probe_id for item in self.probes if item.domain is IdentityDomain.SEMANTIC}
        for row in self.rows:
            named = set(row.moved) | set(row.unmoved)
            unknown = sorted(named - set(by_id))
            if unknown:
                raise CloseoutMatrixError(
                    f"mutation {row.mutation_id!r} names an undeclared probe: {unknown[0]}"
                )
            # Every probe must appear in every row. A row that silently omits a probe is not a
            # weaker claim, it is an unstated one -- and "did not move" is the whole point.
            if named != set(by_id):
                raise CloseoutMatrixError(
                    f"mutation {row.mutation_id!r} does not say what every probe did"
                )
            # Invariant 2: a mutation moves only identities that declare its domain as an input.
            leaked = sorted(name for name in row.moved if row.domain not in by_id[name].inputs)
            if leaked:
                raise CloseoutMatrixError(
                    f"mutation {row.mutation_id!r} in {row.domain.value} moved {leaked[0]!r}, "
                    f"which answers for {by_id[leaked[0]].domain.value}"
                )
            # Invariant 3: no semantic underbinding.
            if row.domain is IdentityDomain.SEMANTIC and not (set(row.moved) & semantic):
                raise CloseoutMatrixError(
                    f"semantic mutation {row.mutation_id!r} moved no semantic identity"
                )

        covered = {row.domain for row in self.rows}
        missing = sorted(domain.value for domain in IdentityDomain if domain not in covered)
        if missing:
            raise CloseoutMatrixError(f"no mutation exercises the {missing[0]} domain")

    @property
    def unmoved_metrics(self) -> tuple[CloseoutMetric, ...]:
        return tuple(item for item in self.metrics if not item.moved)

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for group in (self.probes, self.metrics, self.rows):
            for item in group:
                digest.update(canonical_bytes(item.to_wire()))
                digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "probes": [item.to_wire() for item in self.probes],
            "metrics": [item.to_wire() for item in self.metrics],
            "rows": [item.to_wire() for item in self.rows],
            "fingerprint": self.fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "probe_count": len(self.probes),
            "metric_count": len(self.metrics),
            "unmoved_metrics": len(self.unmoved_metrics),
            "mutation_count": len(self.rows),
            "fingerprint": self.fingerprint,
        }


def observe_blast_radius(
    mutation_id: str,
    domain: IdentityDomain,
    baseline: dict[str, str],
    mutated: dict[str, str],
    *,
    evidence: str,
) -> BlastRadiusRow:
    """Turn two probe readings into a row, deciding what moved rather than being told.

    Both readings must cover exactly the same probes: a probe that appears on one side only is a
    measurement error, not a movement, and inferring either way would put a guess in the record.
    """

    if set(baseline) != set(mutated):
        raise CloseoutMatrixError(
            f"mutation {mutation_id!r} compares different probe sets before and after"
        )
    moved = tuple(sorted(name for name in baseline if baseline[name] != mutated[name]))
    unmoved = tuple(sorted(name for name in baseline if baseline[name] == mutated[name]))
    return BlastRadiusRow(
        mutation_id=mutation_id,
        domain=domain,
        moved=moved,
        unmoved=unmoved,
        evidence=evidence,
    )


__all__ = [
    "CLOSEOUT_MATRIX_SCHEMA",
    "MAX_MATRIX_ENTRIES",
    "MAX_PROBES",
    "BlastRadiusRow",
    "CloseoutMatrix",
    "CloseoutMatrixError",
    "CloseoutMetric",
    "Probe",
    "observe_blast_radius",
]
