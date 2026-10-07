"""Which redundant schemas could be retired, which were, and why the rest were not.

M18-05's rule was written as "retire entries proven to be process-local".  A JSON Schema can never
be proven process-local, because `scripts/contract_inventory.py::_boundary` classifies every schema
`public` before it reaches the process-local branch.  That is a correct conservative default -- a
schema shipped in a wheel is readable by anyone -- but it makes the literal rule unsatisfiable, and
an unsatisfiable rule reads as "we checked and found nothing" when nothing was ever checkable.

So eligibility is stated here about the **pair**: a process-local wire whose shape a public schema
states a second time.  That is what the plan's own Tier E disposition describes -- preserve the
typed value "without a redundant public JSON Schema" -- and it is computable from the inventory.

Two verdicts are kept apart on purpose.  ``eligible`` means the redundancy is real and removable.
``retired`` means it was actually removed, which additionally requires that **nothing consumes it**,
so that deleting the file destroys no test's independent validation evidence.  Recording only the
second would hide the difference between "could go" and "went"; recording only the first would imply
more was removed than was.

The retired list is also the reason a removal is not silent.  A stable ``$id`` that stops shipping
without a record is exactly what a downstream reader cannot diagnose, so each one is kept here with
where it last lived and which commit to restore it from.

This module reads no file and imports no host: it is a pure function of an already-built inventory.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from comfyui_h3_context.core.canonical import canonical_bytes
from comfyui_h3_context.core.contract_inventory import (
    FORBIDDEN_RECORD_TEXT,
    ContractBoundary,
    ContractEntry,
    ContractInventory,
    ContractKind,
    PersistenceClass,
)
from comfyui_h3_context.core.errors import ContractValidationError

RETIREMENT_ELIGIBILITY_SCHEMA = "h3-context-retirement-eligibility/1"
MAX_ELIGIBILITY_ENTRIES = 512
MAX_REASONS = 8
MAX_TEXT = 200
MAX_PATH_LENGTH = 260

#: Consumer roots that do not keep a schema alive for retirement purposes.  A test or a generator
#: reading a schema is evidence the schema is *used*, but it is repository-internal use: it can be
#: replaced by a typed invariant test.  A consumer anywhere else is a reader this item cannot
#: re-migrate, and it blocks retirement outright.
INTERNAL_CONSUMER_ROOTS = ("tests/", "scripts/")

_IDENTITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:/@+-]{0,199}\Z")
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class RetirementEligibilityError(ContractValidationError):
    """Raised when a verdict claims more than the inventory supports."""


class RetirementVerdict(str, Enum):
    """What the evidence says about one schema."""

    #: The redundancy is real and the schema could be removed.
    ELIGIBLE = "eligible"
    #: The redundancy is real but a consumer would lose its validation evidence.
    ELIGIBLE_WITH_CONSUMERS = "eligible_with_consumers"
    #: Something the plan protects blocks it: a boundary, a persistence class, an outside reader.
    BLOCKED = "blocked"
    #: The schema states no inventoried wire's shape, so there is no redundancy to remove.
    NO_REDUNDANCY = "no_redundancy"


def _text(value: object, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RetirementEligibilityError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RetirementEligibilityError(f"{field_name} contains a control character")
    if FORBIDDEN_RECORD_TEXT.search(value):
        raise RetirementEligibilityError(
            f"{field_name} looks like a private path, URL or credential"
        )
    return value


def _relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        raise RetirementEligibilityError(f"{field_name} must be bounded non-empty text")
    if _RELATIVE_PATH.fullmatch(value) is None or ".." in value.split("/"):
        raise RetirementEligibilityError(f"{field_name} must be a safe repository-relative path")
    return value


def _reasons(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_REASONS:
        raise RetirementEligibilityError(f"{field_name} must be a bounded tuple")
    for item in value:
        _text(item, field_name)
    if list(value) != sorted(value):
        raise RetirementEligibilityError(f"{field_name} must be sorted")
    if len(set(value)) != len(value):
        raise RetirementEligibilityError(f"{field_name} contains a duplicate reason")
    return value


def _bounded_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_ELIGIBILITY_ENTRIES:
        raise RetirementEligibilityError(f"{field_name} must be a bounded tuple")
    if list(value) != sorted(set(value)):
        raise RetirementEligibilityError(f"{field_name} must be sorted and unique")
    return value


def _identities(value: object, field_name: str) -> tuple[str, ...]:
    items = _bounded_tuple(value, field_name)
    for item in items:
        if not isinstance(item, str) or _IDENTITY.fullmatch(item) is None:
            raise RetirementEligibilityError(f"{field_name} must be bounded identities")
    return items


def _paths(value: object, field_name: str) -> tuple[str, ...]:
    items = _bounded_tuple(value, field_name)
    for item in items:
        _relative_path(item, field_name)
    return items


@dataclass(frozen=True, slots=True)
class SchemaVerdict:
    """One schema, its verdict, and the reasons that decided it."""

    contract_id: str
    schema_path: str
    verdict: RetirementVerdict
    described_wires: tuple[str, ...]
    internal_consumers: tuple[str, ...]
    external_consumers: tuple[str, ...]
    #: Readers the inventory cannot see.  M18-01 excludes a few files from its scan so the inventory
    #: never supplies its own evidence, and a schema those files read would otherwise show zero
    #: consumers -- a blind spot arriving as a green light.  They are looked at separately and
    #: recorded here, because "the scan found nothing" and "nothing reads it" are different claims.
    hidden_consumers: tuple[str, ...] = ()
    #: Why the verdict is not `eligible`.  Empty exactly when it is, so a reader never has to infer
    #: the absence of an obstacle from the absence of prose.
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or _IDENTITY.fullmatch(self.contract_id) is None:
            raise RetirementEligibilityError("contract_id must be a bounded identity")
        _relative_path(self.schema_path, "schema_path")
        if not isinstance(self.verdict, RetirementVerdict):
            raise RetirementEligibilityError("verdict must be a RetirementVerdict")
        for group, field_name in (
            (self.described_wires, "described_wires"),
            (self.internal_consumers, "internal_consumers"),
            (self.external_consumers, "external_consumers"),
            (self.hidden_consumers, "hidden_consumers"),
        ):
            if not isinstance(group, tuple) or len(group) > MAX_ELIGIBILITY_ENTRIES:
                raise RetirementEligibilityError(f"{field_name} must be a bounded tuple")
            if list(group) != sorted(set(group)):
                raise RetirementEligibilityError(f"{field_name} must be sorted and unique")
        for path in (*self.internal_consumers, *self.external_consumers, *self.hidden_consumers):
            _relative_path(path, "consumer path")
        object.__setattr__(self, "reasons", _reasons(self.reasons, "reasons"))
        if self.verdict is RetirementVerdict.ELIGIBLE and self.reasons:
            raise RetirementEligibilityError("an eligible verdict cannot carry a blocking reason")
        if self.verdict is not RetirementVerdict.ELIGIBLE and not self.reasons:
            raise RetirementEligibilityError("a non-eligible verdict must say what blocked it")

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "schema_path": self.schema_path,
            "verdict": self.verdict.value,
            "described_wires": list(self.described_wires),
            "internal_consumers": list(self.internal_consumers),
            "external_consumers": list(self.external_consumers),
            "hidden_consumers": list(self.hidden_consumers),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class RetirementClaim:
    """What an item declares it removed: identity, where it lived, and how to get it back.

    A claim states **which** file went and nothing about why it was allowed to.  Every reason is
    recomputed by :func:`retire_schema` from the repository as it stands now, so a claim that is
    wrong cannot be recorded -- it raises instead.
    """

    contract_id: str
    schema_path: str
    #: The commit whose tree still contains the file.  Rollback is `git show <commit>:<path>`, which
    #: is the whole point of writing it down: a reader who finds the `$id` gone can get it back.
    rollback_commit: str
    #: The typed module that always decided the shape.  It is also the evidence anchor: the wires
    #: this schema restated are found through it, so a wrong authority yields no wire and fails.
    surviving_authority: str
    item: str

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or _IDENTITY.fullmatch(self.contract_id) is None:
            raise RetirementEligibilityError("contract_id must be a bounded identity")
        _relative_path(self.schema_path, "schema_path")
        if (
            not isinstance(self.rollback_commit, str)
            or _COMMIT.fullmatch(self.rollback_commit) is None
        ):
            raise RetirementEligibilityError("rollback_commit must be a full commit object name")
        _relative_path(self.surviving_authority, "surviving_authority")
        _text(self.item, "item", maximum=32)


@dataclass(frozen=True, slots=True)
class RetiredSchema:
    """A schema that stopped shipping, with the evidence that let it, recomputed.

    A deleted file cannot be re-assessed -- it is gone, so the inventory no longer describes it and
    ``assess_schema`` can never run on it again.  What survives deletion is everything the decision
    actually rested on, and all of it is still measurable:

    * the wires it restated still exist, so their boundary and persistence can be read from the
      inventory today rather than trusted from a note;
    * whether anything still names the identity is a property of the rest of the repository, so the
      zero-consumer rule is re-derivable in full;
    * the typed authority still exists, and is what the wires are found through.

    So the retirement is a checked claim, not an assertion.  Every field below except the five in
    :class:`RetirementClaim` is computed, and every invariant is enforced here: a schema whose wire
    turned out to be persisted, or that something started referencing again, cannot be recorded as
    retired at all.
    """

    contract_id: str
    schema_path: str
    rollback_commit: str
    surviving_authority: str
    item: str
    #: Recomputed: the inventoried wires reached through ``surviving_authority``.
    described_wires: tuple[str, ...] = ()
    #: Recomputed: those wires' trust boundaries.  Every one must be process-local.
    wire_boundaries: tuple[str, ...] = ()
    #: Recomputed: those wires' persistence classes.  Every one must be transient.
    wire_persistence: tuple[str, ...] = ()
    #: Recomputed by the caller, which is the half of the evidence that needs the file tree: every
    #: file that still names this identity.  It must be empty.
    surviving_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or _IDENTITY.fullmatch(self.contract_id) is None:
            raise RetirementEligibilityError("contract_id must be a bounded identity")
        _relative_path(self.schema_path, "schema_path")
        if (
            not isinstance(self.rollback_commit, str)
            or _COMMIT.fullmatch(self.rollback_commit) is None
        ):
            raise RetirementEligibilityError("rollback_commit must be a full commit object name")
        _relative_path(self.surviving_authority, "surviving_authority")
        _text(self.item, "item", maximum=32)
        _identities(self.described_wires, "described_wires")
        # A retirement whose schema restated no surviving wire has no redundancy story at all: the
        # file may still have been dead, but this record cannot say why removing it was safe.
        if not self.described_wires:
            raise RetirementEligibilityError(
                "a retired schema must still name the wire whose shape it restated"
            )
        for values, field_name, allowed in (
            (self.wire_boundaries, "wire_boundaries", {ContractBoundary.PROCESS_LOCAL.value}),
            (self.wire_persistence, "wire_persistence", {PersistenceClass.TRANSIENT.value}),
        ):
            _identities(values, field_name)
            if not values or set(values) - allowed:
                raise RetirementEligibilityError(
                    f"a retired schema's {field_name} must be entirely "
                    + ", ".join(sorted(allowed))
                )
        _paths(self.surviving_references, "surviving_references")
        if self.surviving_references:
            raise RetirementEligibilityError(
                "a retired schema is still referenced and cannot be recorded as retired"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "schema_path": self.schema_path,
            "rollback_commit": self.rollback_commit,
            "surviving_authority": self.surviving_authority,
            "item": self.item,
            "described_wires": list(self.described_wires),
            "wire_boundaries": list(self.wire_boundaries),
            "wire_persistence": list(self.wire_persistence),
            "surviving_references": list(self.surviving_references),
        }


@dataclass(frozen=True, slots=True)
class RetirementEligibility:
    """Every schema's verdict, and every schema that has actually been retired."""

    verdicts: tuple[SchemaVerdict, ...]
    retired: tuple[RetiredSchema, ...] = ()
    schema: str = RETIREMENT_ELIGIBILITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RETIREMENT_ELIGIBILITY_SCHEMA:
            raise RetirementEligibilityError("unsupported retirement eligibility schema")
        if not isinstance(self.verdicts, tuple) or not self.verdicts:
            raise RetirementEligibilityError("verdicts must be a non-empty tuple")
        if len(self.verdicts) > MAX_ELIGIBILITY_ENTRIES:
            raise RetirementEligibilityError("verdicts exceed the bounded surface")
        if not all(isinstance(item, SchemaVerdict) for item in self.verdicts):
            raise RetirementEligibilityError("verdicts contain an invalid value")
        ids = [item.contract_id for item in self.verdicts]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise RetirementEligibilityError("verdicts must be sorted and unique by contract_id")
        if not isinstance(self.retired, tuple) or len(self.retired) > MAX_ELIGIBILITY_ENTRIES:
            raise RetirementEligibilityError("retired must be a bounded tuple")
        if not all(isinstance(item, RetiredSchema) for item in self.retired):
            raise RetirementEligibilityError("retired contains an invalid value")
        retired_ids = [item.contract_id for item in self.retired]
        if retired_ids != sorted(retired_ids) or len(set(retired_ids)) != len(retired_ids):
            raise RetirementEligibilityError("retired must be sorted and unique by contract_id")
        # A retired schema is gone, so the inventory no longer lists it and it cannot also carry a
        # verdict.  The two lists overlapping would mean the record describes a file that both does
        # and does not exist.
        if set(retired_ids) & set(ids):
            raise RetirementEligibilityError("a retired schema cannot also carry a live verdict")

    @property
    def eligible(self) -> tuple[SchemaVerdict, ...]:
        return tuple(
            item
            for item in self.verdicts
            if item.verdict
            in {RetirementVerdict.ELIGIBLE, RetirementVerdict.ELIGIBLE_WITH_CONSUMERS}
        )

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for item in self.verdicts:
            digest.update(canonical_bytes(item.to_wire()))
            digest.update(b"\n")
        for retired in self.retired:
            digest.update(canonical_bytes(retired.to_wire()))
            digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "verdicts": [item.to_wire() for item in self.verdicts],
            "retired": [item.to_wire() for item in self.retired],
            "fingerprint": self.fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for item in self.verdicts:
            counts[item.verdict.value] = counts.get(item.verdict.value, 0) + 1
        return {
            "schema": self.schema,
            "verdict_count": len(self.verdicts),
            "verdicts": dict(sorted(counts.items())),
            "retired_count": len(self.retired),
            "fingerprint": self.fingerprint,
        }


def _schema_stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".schema.json")


def described_wires(
    entry: ContractEntry,
    wires: tuple[ContractEntry, ...],
    *,
    reexported_by: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Wires whose authority module shares the schema's stem, which is how this repo names them.

    `core/reachability.py` is stated a second time by `reachability_v1.schema.json`.  The convention
    is consistent enough to be mechanical, and where it does not hold the schema simply reports no
    redundancy rather than being paired with a wire it does not describe.

    ``reexported_by`` maps a defining module to the aggregation surfaces that re-export it.  Like
    ``hidden_consumers`` it is supplied by the caller because this module reads no file.  It exists
    because M19-05 split three large modules into layers behind a surface, which moved the wire
    constants off the module the schema is named after and silently emptied one schema's wire list.
    The stem convention still holds -- it just now has to be read through the surface.
    """

    stems = {_schema_stem(path) for path in entry.authority_paths}
    surfaces = reexported_by or {}
    found: set[str] = set()
    for wire in wires:
        for path in wire.authority_paths:
            module = path.rsplit("/", 1)[-1].removesuffix(".py")
            candidates = (module, *surfaces.get(module, ()))
            if any(stem.startswith(f"{candidate}_v") for stem in stems for candidate in candidates):
                found.add(wire.contract_id)
    return tuple(sorted(found))


def assess_schema(
    entry: ContractEntry,
    wires: tuple[ContractEntry, ...],
    *,
    hidden_consumers: tuple[str, ...] = (),
    reexported_by: dict[str, tuple[str, ...]] | None = None,
) -> SchemaVerdict:
    """Decide one schema's verdict, counting readers the inventory scan cannot see.

    ``hidden_consumers`` are readers found in files M18-01 deliberately excludes from its scan. They
    are counted exactly like ordinary consumers, because a reader is a reader; the caller supplies
    them because this module reads no file.  Omitting them would let a deliberate blind spot report
    a used schema as safe to delete, which is the one mistake this record must not make.
    """

    if entry.kind is not ContractKind.JSON_SCHEMA:
        raise RetirementEligibilityError("assess_schema requires a json_schema entry")
    paths = sorted(entry.authority_paths)
    wire_ids = described_wires(entry, wires, reexported_by=reexported_by)
    by_id = {wire.contract_id: wire for wire in wires}
    hidden = tuple(sorted(set(hidden_consumers)))
    everyone = (*entry.consumers, *hidden)
    internal = tuple(
        sorted({path for path in everyone if path.startswith(INTERNAL_CONSUMER_ROOTS)})
    )
    external = tuple(
        sorted({path for path in everyone if not path.startswith(INTERNAL_CONSUMER_ROOTS)})
    )

    reasons: list[str] = []
    if not wire_ids:
        reasons.append("states no inventoried wire's shape, so there is no redundancy to remove")
    protected = sorted(
        {
            by_id[wire_id].boundary.value
            for wire_id in wire_ids
            if by_id[wire_id].boundary is not ContractBoundary.PROCESS_LOCAL
        }
    )
    if protected:
        reasons.append("states a wire on the protected boundary " + ", ".join(protected))
    if entry.persistence is not PersistenceClass.TRANSIENT:
        reasons.append(f"the schema itself is {entry.persistence.value}")
    if external:
        reasons.append(f"{len(external)} reader outside tests and scripts would lose it")

    if reasons:
        verdict = (
            RetirementVerdict.NO_REDUNDANCY
            if not wire_ids and len(reasons) == 1
            else RetirementVerdict.BLOCKED
        )
    elif internal:
        verdict = RetirementVerdict.ELIGIBLE_WITH_CONSUMERS
        reasons.append(
            f"{len(internal)} repository-internal reader would lose its validation evidence"
        )
    else:
        verdict = RetirementVerdict.ELIGIBLE

    return SchemaVerdict(
        contract_id=entry.contract_id,
        schema_path=paths[0],
        verdict=verdict,
        described_wires=wire_ids,
        internal_consumers=internal,
        external_consumers=external,
        hidden_consumers=hidden,
        reasons=tuple(sorted(reasons)),
    )


def retire_schema(
    claim: RetirementClaim,
    wires: tuple[ContractEntry, ...],
    *,
    surviving_references: tuple[str, ...] = (),
) -> RetiredSchema:
    """Turn a declaration that a schema was removed into a checked record of why that was allowed.

    The claim supplies identity and rollback; everything else is measured here.  The wires are found
    through the claimed surviving authority rather than through the schema's own name, because the
    schema is gone -- and that makes the authority load-bearing: name the wrong module and no wire
    is found, which :class:`RetiredSchema` refuses.
    """

    if not isinstance(claim, RetirementClaim):
        raise RetirementEligibilityError("retire_schema requires a RetirementClaim")
    described = tuple(
        sorted(
            {
                wire.contract_id
                for wire in wires
                if claim.surviving_authority in wire.authority_paths
            }
        )
    )
    by_id = {wire.contract_id: wire for wire in wires}
    return RetiredSchema(
        contract_id=claim.contract_id,
        schema_path=claim.schema_path,
        rollback_commit=claim.rollback_commit,
        surviving_authority=claim.surviving_authority,
        item=claim.item,
        described_wires=described,
        wire_boundaries=tuple(sorted({by_id[name].boundary.value for name in described})),
        wire_persistence=tuple(sorted({by_id[name].persistence.value for name in described})),
        surviving_references=tuple(sorted(set(surviving_references))),
    )


def build_retirement_eligibility(
    inventory: ContractInventory,
    *,
    claims: tuple[RetirementClaim, ...] = (),
    hidden_consumers: dict[str, tuple[str, ...]] | None = None,
    surviving_references: dict[str, tuple[str, ...]] | None = None,
    reexported_by: dict[str, tuple[str, ...]] | None = None,
) -> RetirementEligibility:
    """Assess every inventoried JSON Schema, in one order, from one inventory.

    ``surviving_references`` names, per retired identity, every file that still mentions it.  Like
    ``hidden_consumers`` it is supplied by the caller because this module reads no file, and like
    ``hidden_consumers`` it exists so a claim is checked rather than believed: a non-empty entry
    stops the record from being built at all.
    """

    if not isinstance(inventory, ContractInventory):
        raise RetirementEligibilityError(
            "build_retirement_eligibility requires a ContractInventory"
        )
    hidden = hidden_consumers or {}
    references = surviving_references or {}
    wires = tuple(entry for entry in inventory.entries if entry.kind is ContractKind.PYTHON_WIRE)
    verdicts = tuple(
        sorted(
            (
                assess_schema(
                    entry,
                    wires,
                    hidden_consumers=hidden.get(entry.contract_id, ()),
                    reexported_by=reexported_by,
                )
                for entry in inventory.entries
                if entry.kind is ContractKind.JSON_SCHEMA
            ),
            key=lambda item: item.contract_id,
        )
    )
    retired = tuple(
        sorted(
            (
                retire_schema(
                    claim,
                    wires,
                    surviving_references=references.get(claim.contract_id, ()),
                )
                for claim in claims
            ),
            key=lambda item: item.contract_id,
        )
    )
    return RetirementEligibility(verdicts=verdicts, retired=retired)


__all__ = [
    "INTERNAL_CONSUMER_ROOTS",
    "MAX_ELIGIBILITY_ENTRIES",
    "RETIREMENT_ELIGIBILITY_SCHEMA",
    "RetiredSchema",
    "RetirementClaim",
    "RetirementEligibility",
    "RetirementEligibilityError",
    "RetirementVerdict",
    "SchemaVerdict",
    "assess_schema",
    "build_retirement_eligibility",
    "described_wires",
    "retire_schema",
]
