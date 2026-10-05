"""Deterministic contract-surface inventory and boundary classification for M18-01.

The inventory answers one question per contract: who owns it, who reads it, what boundary it sits
on, and may it be simplified or retired.  It records; it never changes a wire value, a fingerprint
or a schema.  A fact this module cannot establish is `NEED_EVIDENCE`, an honest result that
prohibits both a runtime-correctness claim and a safe-retirement claim -- the absence of a grep hit
has never been evidence that a contract is unused.

Runtime reachability is *not* modelled here.  `core/reachability.py` already owns that vocabulary,
including the rule that an injected fixture is never a normal producer, and a second vocabulary for
the same question would be exactly the redundant authority this chain exists to remove.  The entries
below reuse its two enumerations and add only the facets it does not model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from .canonical import canonical_bytes
from .errors import ContractValidationError
from .reachability import ReachabilityDisposition, ReachabilitySource

CONTRACT_INVENTORY_SCHEMA = "h3-context-contract-inventory/1"
MAX_INVENTORY_ENTRIES = 1_024
MAX_AUTHORITY_PATHS = 32
MAX_EDGES = 256
MAX_BLOCKERS = 8
MAX_TEXT = 240
MAX_PATH_LENGTH = 260

_CONTRACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}\Z")
#: Repository-relative, forward slashes only, and a segment may contain a space because two
#: packaged subgraph fixtures do.  Leading, trailing and doubled separators are still refused,
#: and an absolute or UNC path is caught by the forbidden-text rule below.
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")
# SECURITY: the inventory ships in the package, so a value shaped like a private path, a credential
# or a user-supplied URL must fail closed here rather than be reviewed for later.
_EnumT = TypeVar("_EnumT", bound=Enum)

#: One authority for "this value must never appear in a record this repository ships".
#: Anchored at the start for the path forms, because the realistic failure is a value that
#: *is* an absolute or UNC path, not an English sentence mentioning one.  Imported by
#: `core/fingerprint_domain.py` rather than copied: a second regex for one rule drifts the
#: first time either side is tightened.
FORBIDDEN_RECORD_TEXT = re.compile(
    r"(?i)(?:^[A-Za-z]:[\\/]|^[\\/]{2}|-----BEGIN|authorization\s*:|cookie\s*:|"
    r"api[_-]?key\s*[:=]|secret\s*[:=]|token\s*[:=]|signed[_-]?url|https?://)"
)


class ContractInventoryError(ContractValidationError):
    """Raised when an inventory entry is incomplete, unsafe, or claims more than its evidence."""


class ContractKind(str, Enum):
    """What sort of authority an entry describes."""

    JSON_SCHEMA = "json_schema"
    JSON_DATA = "json_data"
    PYTHON_WIRE = "python_wire"
    FRONTEND_CODEC = "frontend_codec"
    WORKFLOW_FIXTURE = "workflow_fixture"
    #: A node type this repository pins and consumes but does not own; see `ContractBoundary`.
    HOST_NODE = "host_node"
    PACKAGE_METADATA = "package_metadata"


class ContractBoundary(str, Enum):
    """The trust and ownership boundary a contract sits on.

    `HOST_CORE` is not this repository's: a workflow fixture carries ComfyUI-core node types that
    are host contracts, and they are recorded so their presence is explained, never as repo-owned
    authorities and never as retirement candidates of this repository.
    """

    PUBLIC = "public"
    PERSISTED = "persisted"
    CROSS_LANGUAGE = "cross_language"
    UNTRUSTED_INPUT = "untrusted_input"
    PROCESS_LOCAL = "process_local"
    TEST_ONLY = "test_only"
    HOST_CORE = "host_core"


#: Boundaries a contract may never be retired from directly (AC-M18-01-04), whatever an override
#: says.  Each of these has a reader this repository does not control or cannot re-migrate.
PROTECTED_BOUNDARIES = frozenset(
    {
        ContractBoundary.PUBLIC,
        ContractBoundary.PERSISTED,
        ContractBoundary.CROSS_LANGUAGE,
        ContractBoundary.UNTRUSTED_INPUT,
        ContractBoundary.HOST_CORE,
    }
)


class PersistenceClass(str, Enum):
    PERSISTED = "persisted"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class PublicationStatus(str, Enum):
    PACKAGED = "packaged"
    REPOSITORY_ONLY = "repository_only"
    UNKNOWN = "unknown"


class ContractDisposition(str, Enum):
    KEEP = "KEEP"
    SIMPLIFY = "SIMPLIFY"
    DEPRECATE = "DEPRECATE"
    RETIRE_CANDIDATE = "RETIRE_CANDIDATE"
    NEED_EVIDENCE = "NEED_EVIDENCE"
    BLOCKED_CORRECTION_REQUIRED = "BLOCKED_CORRECTION_REQUIRED"


class CompatibilityDisposition(str, Enum):
    NO_READERS = "no_readers"
    OLD_READERS_UNAFFECTED = "old_readers_unaffected"
    MIGRATION_REQUIRED = "migration_required"
    UNKNOWN = "unknown"


class InventoryBlocker(str, Enum):
    """A recorded obstruction.  A blocked entry never claims a disposition it cannot support."""

    DUPLICATE_SCHEMA_IDENTITY = "duplicate_schema_identity"
    UNRESOLVED_OWNER = "unresolved_owner"
    UNKNOWN_CONSUMER = "unknown_consumer"
    INCOMPLETE_POSITION_BINDING = "incomplete_position_binding"


#: Dispositions an entry carrying any blocker may hold.  A blocker must never coexist with a claim
#: that the contract is fine as it is or safe to drop.
_BLOCKED_DISPOSITIONS = frozenset(
    {ContractDisposition.NEED_EVIDENCE, ContractDisposition.BLOCKED_CORRECTION_REQUIRED}
)


def _text(value: object, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractInventoryError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ContractInventoryError(f"{field_name} contains a control character")
    if FORBIDDEN_RECORD_TEXT.search(value):
        raise ContractInventoryError(f"{field_name} looks like a private path, URL or credential")
    return value


#: JSON Schema dialects are standards URIs, so they are checked against a closed set rather than
#: through the URL rule that keeps user-supplied links out of a shipped artifact.
KNOWN_DIALECTS = frozenset(
    {
        "not_applicable",
        "unstated",
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2019-09/schema",
        "http://json-schema.org/draft-07/schema#",
    }
)


def _dialect(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value not in KNOWN_DIALECTS:
        raise ContractInventoryError(f"{field_name} must be a known JSON Schema dialect")
    return value


#: The identity namespaces this repository actually declares.  A `$id` is a repository-owned URI,
#: not user data, so it is admitted by an explicit prefix rather than by the general URL rule -- and
#: an identity pointing anywhere else still fails closed.  The list is deliberately a list: eight
#: namespaces for eighty-eight schemas is itself an inventory finding, not a thing to normalise away
#: here, because changing a `$id` is a wire change this item is forbidden to make.
KNOWN_IDENTITY_NAMESPACES = (
    "comfyui-h3-context://",
    "h3-context://",
    "https://comfyui-h3-context.invalid/",
    "https://comfyui-h3-context.local/",
    "https://example.invalid/",
    "https://rookiestar28.github.io/ComfyUI-MinimaxH3-Context/",
)


def _contract_id(value: object, field_name: str) -> str:
    if isinstance(value, str) and value.startswith(KNOWN_IDENTITY_NAMESPACES):
        text = value
        if len(text) > 160 or any(ord(character) < 0x20 for character in text):
            raise ContractInventoryError(f"{field_name} must be a bounded contract identifier")
    else:
        text = _text(value, field_name, maximum=160)
    if _CONTRACT_ID.fullmatch(text) is None:
        raise ContractInventoryError(f"{field_name} must be a bounded contract identifier")
    return text


def _relative_path(value: object, field_name: str) -> str:
    text = _text(value, field_name, maximum=MAX_PATH_LENGTH)
    if _RELATIVE_PATH.fullmatch(text) is None or ".." in text.split("/"):
        raise ContractInventoryError(f"{field_name} must be a safe repository-relative path")
    return text


def _paths(value: object, field_name: str, *, maximum: int, required: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > maximum:
        raise ContractInventoryError(f"{field_name} must be a bounded tuple")
    if required and not value:
        raise ContractInventoryError(f"{field_name} must not be empty")
    paths = tuple(_relative_path(item, f"{field_name} entry") for item in value)
    if len(set(paths)) != len(paths):
        raise ContractInventoryError(f"{field_name} contains a duplicate path")
    if list(paths) != sorted(paths):
        raise ContractInventoryError(f"{field_name} must be sorted")
    return paths


def _enum(value: object, expected: type[_EnumT], field_name: str) -> _EnumT:
    if not isinstance(value, expected):
        raise ContractInventoryError(f"{field_name} must be a {expected.__name__}")
    return value


@dataclass(frozen=True, slots=True)
class ContractEntry:
    """One contract, everything known about it, and the rule that decided its disposition."""

    contract_id: str
    kind: ContractKind
    authority_paths: tuple[str, ...]
    dialect: str
    producers: tuple[str, ...]
    consumers: tuple[str, ...]
    boundary: ContractBoundary
    persistence: PersistenceClass
    publication: PublicationStatus
    #: Present only where the question `core/reachability.py` asks actually applies -- a route,
    #: a node-facing contract or a fixture-only surface.  An ordinary internal wire is not
    #: "unsupported"; the question is simply not about it, and saying nothing is the honest answer.
    reachability: ReachabilityDisposition | None
    reachability_source: ReachabilitySource | None
    fingerprint_domain: str
    disposition: ContractDisposition
    compatibility: CompatibilityDisposition
    evidence: str
    blockers: tuple[InventoryBlocker, ...] = ()
    deferred_consumer_note: str | None = None

    def __post_init__(self) -> None:
        _contract_id(self.contract_id, "contract_id")
        _enum(self.kind, ContractKind, "kind")
        object.__setattr__(
            self,
            "authority_paths",
            _paths(
                self.authority_paths, "authority_paths", maximum=MAX_AUTHORITY_PATHS, required=True
            ),
        )
        _dialect(self.dialect, "dialect")
        object.__setattr__(
            self,
            "producers",
            _paths(self.producers, "producers", maximum=MAX_EDGES, required=False),
        )
        object.__setattr__(
            self,
            "consumers",
            _paths(self.consumers, "consumers", maximum=MAX_EDGES, required=False),
        )
        _enum(self.boundary, ContractBoundary, "boundary")
        _enum(self.persistence, PersistenceClass, "persistence")
        _enum(self.publication, PublicationStatus, "publication")
        if (self.reachability is None) != (self.reachability_source is None):
            raise ContractInventoryError(
                "a reachability claim and its source are recorded together or not at all"
            )
        if self.reachability is not None:
            _enum(self.reachability, ReachabilityDisposition, "reachability")
            _enum(self.reachability_source, ReachabilitySource, "reachability_source")
        _text(self.fingerprint_domain, "fingerprint_domain", maximum=120)
        _enum(self.disposition, ContractDisposition, "disposition")
        _enum(self.compatibility, CompatibilityDisposition, "compatibility")
        _text(self.evidence, "evidence")
        self._validate_blockers()
        self._validate_claims()
        if self.deferred_consumer_note is not None:
            _text(self.deferred_consumer_note, "deferred_consumer_note")

    def _validate_blockers(self) -> None:
        if not isinstance(self.blockers, tuple) or len(self.blockers) > MAX_BLOCKERS:
            raise ContractInventoryError("blockers must be a bounded tuple")
        for item in self.blockers:
            _enum(item, InventoryBlocker, "blocker")
        values = [item.value for item in self.blockers]
        if len(set(values)) != len(values):
            raise ContractInventoryError("blockers contain a duplicate")
        if list(values) != sorted(values):
            raise ContractInventoryError("blockers must be sorted")

    def _validate_claims(self) -> None:
        if self.blockers and self.disposition not in _BLOCKED_DISPOSITIONS:
            raise ContractInventoryError(
                "a blocked contract cannot claim a disposition its evidence does not support"
            )
        # AC-M18-01-04: a reader this repository does not control cannot be retired past.
        if (
            self.disposition is ContractDisposition.RETIRE_CANDIDATE
            and self.boundary in PROTECTED_BOUNDARIES
        ):
            raise ContractInventoryError(
                f"a {self.boundary.value} contract cannot be a direct retirement candidate"
            )
        # AC-M18-01-03: no discovered consumer is not proof of disuse.
        if not self.consumers and self.disposition not in _BLOCKED_DISPOSITIONS:
            raise ContractInventoryError(
                "a contract with no discovered consumer must fail closed as NEED_EVIDENCE"
            )
        # AC-M18-01-07: an injected fixture is evidence about a test, not about a runtime.
        if (
            self.reachability_source is ReachabilitySource.INJECTED_FIXTURE
            and self.reachability is ReachabilityDisposition.REACHABLE
        ):
            raise ContractInventoryError(
                "an injected fixture never establishes runtime reachability"
            )
        if (
            self.reachability is ReachabilityDisposition.REACHABLE
            and self.reachability_source is ReachabilitySource.NONE
        ):
            raise ContractInventoryError(
                "a reachable contract must name the source that reaches it"
            )
        # This repository does not own a host contract, so it cannot dispose of one either.
        if self.boundary is ContractBoundary.HOST_CORE and self.disposition not in {
            ContractDisposition.KEEP,
            ContractDisposition.NEED_EVIDENCE,
        }:
            raise ContractInventoryError("a host-core contract is recorded, never dispositioned")

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.kind.value, self.contract_id

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "contract_id": self.contract_id,
            "kind": self.kind.value,
            "authority_paths": list(self.authority_paths),
            "dialect": self.dialect,
            "producers": list(self.producers),
            "consumers": list(self.consumers),
            "boundary": self.boundary.value,
            "persistence": self.persistence.value,
            "publication": self.publication.value,
            "fingerprint_domain": self.fingerprint_domain,
            "disposition": self.disposition.value,
            "compatibility": self.compatibility.value,
            "evidence": self.evidence,
            "blockers": [item.value for item in self.blockers],
        }
        if self.reachability is not None and self.reachability_source is not None:
            wire["reachability"] = self.reachability.value
            wire["reachability_source"] = self.reachability_source.value
        if self.deferred_consumer_note is not None:
            wire["deferred_consumer_note"] = self.deferred_consumer_note
        return wire


@dataclass(frozen=True, slots=True)
class ContractInventory:
    """The whole surface, once, in a deterministic order."""

    entries: tuple[ContractEntry, ...] = field(default=())
    schema: str = CONTRACT_INVENTORY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CONTRACT_INVENTORY_SCHEMA:
            raise ContractInventoryError("unsupported contract inventory schema")
        if (
            not isinstance(self.entries, tuple)
            or not self.entries
            or len(self.entries) > MAX_INVENTORY_ENTRIES
        ):
            raise ContractInventoryError("inventory entries are outside the bounded limit")
        if not all(isinstance(item, ContractEntry) for item in self.entries):
            raise ContractInventoryError("inventory entries contain an invalid value")
        keys = [item.sort_key for item in self.entries]
        # AC-M18-01-01: exactly once, and in one order, so a diff of the artifact is readable.
        if len(set(keys)) != len(keys):
            raise ContractInventoryError("a contract appears more than once in the inventory")
        if keys != sorted(keys):
            raise ContractInventoryError("inventory entries must be sorted by kind and identity")

    @property
    def fingerprint(self) -> str:
        """Fold the per-entry canonical bytes rather than canonicalising one huge array.

        `canonical_fingerprint` bounds a collection at 256 items because a product wire that large
        is a defect. A repository-wide inventory is legitimately larger, so each entry is
        canonicalised on its own -- keeping the same normalisation and the same refusals -- and the
        digest is folded over them in the order the inventory already guarantees.
        """

        digest = hashlib.sha256()
        for item in self.entries:
            digest.update(canonical_bytes(item.to_wire()))
            digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def by_disposition(self, disposition: ContractDisposition) -> tuple[ContractEntry, ...]:
        return tuple(item for item in self.entries if item.disposition is disposition)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "entries": [item.to_wire() for item in self.entries],
            "fingerprint": self.fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for item in self.entries:
            counts[item.disposition.value] = counts.get(item.disposition.value, 0) + 1
        return {
            "schema": self.schema,
            "entry_count": len(self.entries),
            "dispositions": dict(sorted(counts.items())),
            "blocked_entries": sorted(item.contract_id for item in self.entries if item.blockers),
            "fingerprint": self.fingerprint,
        }


def build_contract_inventory(entries: tuple[ContractEntry, ...]) -> ContractInventory:
    """Sort and validate one complete inventory; a caller never has to pre-sort."""

    if not isinstance(entries, tuple):
        raise ContractInventoryError("entries must be a tuple")
    if not all(isinstance(item, ContractEntry) for item in entries):
        raise ContractInventoryError("entries contain an invalid value")
    return ContractInventory(entries=tuple(sorted(entries, key=lambda item: item.sort_key)))


__all__ = [
    "CONTRACT_INVENTORY_SCHEMA",
    "FORBIDDEN_RECORD_TEXT",
    "KNOWN_DIALECTS",
    "KNOWN_IDENTITY_NAMESPACES",
    "MAX_INVENTORY_ENTRIES",
    "PROTECTED_BOUNDARIES",
    "CompatibilityDisposition",
    "ContractBoundary",
    "ContractDisposition",
    "ContractEntry",
    "ContractInventory",
    "ContractInventoryError",
    "ContractKind",
    "InventoryBlocker",
    "PersistenceClass",
    "PublicationStatus",
    "build_contract_inventory",
]
