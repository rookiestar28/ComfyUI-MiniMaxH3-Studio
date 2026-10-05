"""Versioned fingerprint domains, and the identities that must not be mistaken for each other.

A SHA-256 digest looks the same whatever it was computed over, so nothing in a bare `str` stops a
supply-chain digest from being handed to a cache lookup, or a projection identity from deciding
whether a segment may be reused.  This module makes the domain part of the value: a new identity is
produced by :func:`domain_fingerprint`, which frames the domain and its version *before* the
canonical bytes are hashed, and it is returned as a :class:`DomainFingerprint` that names its own
domain.  A consumer that needs a runtime semantic identity then asks for one
(:func:`require_semantic_identity`) and is refused anything else.

Two boundaries are deliberate.

The helper is **additive**.  It is used by identities introduced from M18-02 onward; the 289
existing fingerprint sites keep their exact bytes.  Threading a domain tag through them would change
every fingerprint value in the repository -- the outcome the M18-02 plan prohibits, because it
invalidates accepted artifacts, caches and receipts.  Existing identities gain a *recorded* domain
in :class:`FingerprintDomainAssignment`, not a new value.

The assignment record is **content-free**.  It names contracts, paths and rules; never a prompt, a
media value, a locator or a credential.  It ships inside the package, so a value shaped like a
private path or a secret fails closed here rather than being reviewed later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from .canonical import canonical_bytes, canonical_fingerprint
from .contract_inventory import FORBIDDEN_RECORD_TEXT, KNOWN_IDENTITY_NAMESPACES
from .errors import ContractValidationError

FINGERPRINT_DOMAIN_SCHEMA = "h3-context-fingerprint-domain/1"
FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA = "h3-context-fingerprint-domain-assignment/1"
CURRENT_FINGERPRINT_DOMAIN_VERSION = 1
MAX_FINGERPRINT_DOMAIN_VERSION = 99
MAX_ASSIGNMENTS = 1_024
MAX_AUTHORITY_PATHS = 32
MAX_TEXT = 240
MAX_PATH_LENGTH = 260

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONSUMER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}\Z")
_RULE_NAME = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_PATH_SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
_RELATIVE_PATH = re.compile(rf"{_PATH_SEGMENT}(?:/{_PATH_SEGMENT})*\Z")

_EnumT = TypeVar("_EnumT", bound=Enum)


class FingerprintDomainError(ContractValidationError):
    """Raised when a domain is misused, or an assignment claims more than its rule supports."""


class IdentityDomain(str, Enum):
    """What an identity is *for*.  The inclusion rules are the frozen M18-02 matrix.

    `SEMANTIC` is the only domain a runtime may consume as an execution, cache or receipt identity.
    The other four exist so that a digest computed for a different question cannot silently answer
    that one.

    NAMED `IdentityDomain` RATHER THAN `FingerprintDomain` BECAUSE THAT NAME IS TAKEN.
    `core/generation_sequence.py` exports a `FingerprintDomain` whose members are `CONTEXT_SUBGRAPH`
    and `OUTPUT_PRODUCING_GRAPH` -- a different axis entirely: *which part of the graph* a recompute
    identity covers, where both members are semantic identities under this vocabulary.  Renaming an
    accepted M17 public export for a naming preference is outside this item, so the collision is
    recorded here and belongs to M19-02, which owns core public API and export reduction.
    """

    #: Fields that decide execution, cache reuse or a receipt: prompt text, intent graph,
    #: generation profile, model and provider identity, settings that change output.
    SEMANTIC = "semantic"
    #: The structural shape of a typed contract: schema identity, field set, version framing.
    #: Never the instance values that contract carries.
    WIRE_CONTRACT = "wire_contract"
    #: Projection and UI identity used to detect a stale render.  Never an execution or cache
    #: identity, however convenient the value would be as one.
    PRESENTATION = "presentation"
    #: Test and workflow fixture identity.  A fixture identity never authorizes reuse.
    FIXTURE = "fixture"
    #: Supply-chain and package integrity: bundle digests, SBOM, license inventory, pinned upstream
    #: revisions.  Complete, and refused where a semantic or cache identity is required.
    RELEASE_INTEGRITY = "release_integrity"


#: Every domain except `SEMANTIC`.  Named rather than derived at each call site so the rule
#: reads the same way in the enforcement, the tests and the shipped schema.
NON_SEMANTIC_DOMAINS = frozenset(IdentityDomain) - {IdentityDomain.SEMANTIC}


class FingerprintConsumerKind(str, Enum):
    """Whether an assignment is about an inventoried contract or a named place in the code.

    Both are needed and neither substitutes for the other.  `CONTRACT` covers the M18-01
    inventory, which is what AC-M18-02-01 counts.  `IDENTITY_SITE` covers a specific computation
    whose domain is not a property of any one contract -- the browser's graph-projection digest is
    computed over a live graph object, and the sidebar prompt agreement is one field inside a
    projection whose other fields answer a different question.
    """

    CONTRACT = "contract"
    IDENTITY_SITE = "identity_site"


def _version(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FingerprintDomainError(f"{field_name} must be an integer")
    if not 1 <= value <= MAX_FINGERPRINT_DOMAIN_VERSION:
        raise FingerprintDomainError(f"{field_name} is outside the supported version range")
    return value


def _enum(value: object, expected: type[_EnumT], field_name: str) -> _EnumT:
    if not isinstance(value, expected):
        raise FingerprintDomainError(f"{field_name} must be a {expected.__name__}")
    return value


def _text(value: object, field_name: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FingerprintDomainError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FingerprintDomainError(f"{field_name} contains a control character")
    if FORBIDDEN_RECORD_TEXT.search(value):
        raise FingerprintDomainError(f"{field_name} looks like a private path, URL or credential")
    return value


def _consumer_id(value: object, field_name: str) -> str:
    """Admit a repository-owned `$id` URI by prefix, and everything else by the general text rule.

    `KNOWN_IDENTITY_NAMESPACES` is imported rather than restated: which URI namespaces this
    repository declares is one fact, and the M18-01 inventory already owns it.  A second copy here
    would be exactly the redundant authority the M18 chain exists to remove, and it would drift the
    first time a namespace is added.
    """

    if isinstance(value, str) and value.startswith(KNOWN_IDENTITY_NAMESPACES):
        text = value
        if len(text) > 160 or any(ord(character) < 0x20 for character in text):
            raise FingerprintDomainError(f"{field_name} must be a bounded consumer identifier")
    else:
        text = _text(value, field_name, maximum=160)
    if _CONSUMER_ID.fullmatch(text) is None:
        raise FingerprintDomainError(f"{field_name} must be a bounded consumer identifier")
    return text


def _relative_path(value: object, field_name: str) -> str:
    text = _text(value, field_name, maximum=MAX_PATH_LENGTH)
    if _RELATIVE_PATH.fullmatch(text) is None or ".." in text.split("/"):
        raise FingerprintDomainError(f"{field_name} must be a safe repository-relative path")
    return text


def domain_label(domain: IdentityDomain, version: int = CURRENT_FINGERPRINT_DOMAIN_VERSION) -> str:
    """Render the exact `<domain>/v<n>` label that appears in framing and in the record."""

    return f"{_enum(domain, IdentityDomain, 'domain').value}/v{_version(version, 'version')}"


@dataclass(frozen=True, slots=True)
class DomainFingerprint:
    """One identity that knows which question it answers.

    The digest is carried alongside its domain rather than returned bare, because that is the whole
    mechanism: a caller cannot pass this where a semantic identity is required without the refusal
    in :func:`require_semantic_identity` seeing the domain.
    """

    domain: IdentityDomain
    domain_version: int
    digest: str

    def __post_init__(self) -> None:
        _enum(self.domain, IdentityDomain, "domain")
        _version(self.domain_version, "domain_version")
        if not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None:
            raise FingerprintDomainError("digest must be a prefixed lowercase SHA-256 value")

    @property
    def label(self) -> str:
        return domain_label(self.domain, self.domain_version)

    def to_wire(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "domain_version": self.domain_version,
            "digest": self.digest,
        }


def domain_fingerprint(
    domain: IdentityDomain,
    value: object,
    *,
    version: int = CURRENT_FINGERPRINT_DOMAIN_VERSION,
) -> DomainFingerprint:
    """Frame the domain and its version, then hash the canonical bytes of the framed projection.

    Framing happens before hashing rather than after, so the same value under two domains -- or
    the same domain at two versions -- produces two different digests.  A domain tag appended to a
    digest would be advisory; a domain folded into the hashed bytes is not.
    """

    framed = {
        "domain": _enum(domain, IdentityDomain, "domain").value,
        "domain_version": _version(version, "version"),
        "schema": FINGERPRINT_DOMAIN_SCHEMA,
        "value": value,
    }
    return DomainFingerprint(domain, version, canonical_fingerprint(framed))


def require_semantic_identity(
    identity: DomainFingerprint,
    *,
    purpose: str = "a runtime semantic identity",
) -> str:
    """Return the digest of a semantic identity, refusing every other domain (AC-M18-02-03).

    A release-integrity digest stays complete and usable for what it is for; what it may not do is
    stand in for an execution, cache or receipt identity.  The same refusal covers presentation,
    fixture and wire-contract identities, because none of them measures the fields that decide
    output either.
    """

    if not isinstance(identity, DomainFingerprint):
        raise FingerprintDomainError(f"{purpose} requires a DomainFingerprint")
    if identity.domain is not IdentityDomain.SEMANTIC:
        raise FingerprintDomainError(
            f"a {identity.domain.value} identity cannot be consumed as {purpose}"
        )
    return identity.digest


@dataclass(frozen=True, slots=True)
class FingerprintDomainAssignment:
    """One consumer, the single domain it is assigned, and the named rule that assigned it.

    `rule` is not decoration.  Every assignment is made by a rule with a stated criterion, so a
    reader can check the classification against the criterion instead of against an opinion, and a
    consumer that matches no rule fails generation rather than defaulting into a domain.
    """

    consumer_id: str
    kind: FingerprintConsumerKind
    domain: IdentityDomain
    domain_version: int
    authority_paths: tuple[str, ...]
    rule: str
    evidence: str
    #: The counterpart runtime an identity must agree with, when one exists.  `None` records the
    #: commoner and more important fact: this identity has no cross-language partner and must never
    #: be compared against one.
    cross_language_partner: str | None = None

    def __post_init__(self) -> None:
        _consumer_id(self.consumer_id, "consumer_id")
        _enum(self.kind, FingerprintConsumerKind, "kind")
        _enum(self.domain, IdentityDomain, "domain")
        _version(self.domain_version, "domain_version")
        if not isinstance(self.authority_paths, tuple) or not self.authority_paths:
            raise FingerprintDomainError("authority_paths must be a non-empty tuple")
        if len(self.authority_paths) > MAX_AUTHORITY_PATHS:
            raise FingerprintDomainError("authority_paths exceeds the bounded limit")
        paths = tuple(
            _relative_path(item, "authority_paths entry") for item in self.authority_paths
        )
        if len(set(paths)) != len(paths):
            raise FingerprintDomainError("authority_paths contains a duplicate path")
        if list(paths) != sorted(paths):
            raise FingerprintDomainError("authority_paths must be sorted")
        object.__setattr__(self, "authority_paths", paths)
        rule = _text(self.rule, "rule", maximum=64)
        if _RULE_NAME.fullmatch(rule) is None:
            raise FingerprintDomainError("rule must be a lowercase snake_case rule name")
        _text(self.evidence, "evidence")
        if self.cross_language_partner is not None:
            _relative_path(self.cross_language_partner, "cross_language_partner")

    @property
    def label(self) -> str:
        return domain_label(self.domain, self.domain_version)

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.kind.value, self.consumer_id

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {
            "consumer_id": self.consumer_id,
            "kind": self.kind.value,
            "domain": self.domain.value,
            "domain_version": self.domain_version,
            "authority_paths": list(self.authority_paths),
            "rule": self.rule,
            "evidence": self.evidence,
        }
        if self.cross_language_partner is not None:
            wire["cross_language_partner"] = self.cross_language_partner
        return wire


@dataclass(frozen=True, slots=True)
class FingerprintDomainAssignmentSet:
    """Every assigned consumer, once, in one order.

    The "once" is the enforced half of AC-M18-02-01: a consumer named twice is a consumer with two
    domains, and there is no reading of that which is safe, so it is refused here rather than
    resolved by a precedence rule nobody would remember.
    """

    assignments: tuple[FingerprintDomainAssignment, ...] = field(default=())
    schema: str = FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA:
            raise FingerprintDomainError("unsupported fingerprint domain assignment schema")
        if (
            not isinstance(self.assignments, tuple)
            or not self.assignments
            or len(self.assignments) > MAX_ASSIGNMENTS
        ):
            raise FingerprintDomainError("assignments are outside the bounded limit")
        if not all(isinstance(item, FingerprintDomainAssignment) for item in self.assignments):
            raise FingerprintDomainError("assignments contain an invalid value")
        identities = [item.consumer_id for item in self.assignments]
        if len(set(identities)) != len(identities):
            raise FingerprintDomainError("a consumer is assigned more than one domain")
        keys = [item.sort_key for item in self.assignments]
        if keys != sorted(keys):
            raise FingerprintDomainError("assignments must be sorted by kind and consumer")

    @property
    def fingerprint(self) -> str:
        """Fold per-assignment canonical bytes, as the M18-01 inventory does and for one reason.

        `canonical_fingerprint` bounds a collection at 256 items because a product wire that
        large is a defect.  A repository-wide record is legitimately larger, so each row is
        canonicalised on its own -- same normalisation, same refusals -- and the digest folds
        over the order the set already guarantees.
        """

        digest = hashlib.sha256()
        digest.update(canonical_bytes({"schema": self.schema}))
        digest.update(b"\n")
        for item in self.assignments:
            digest.update(canonical_bytes(item.to_wire()))
            digest.update(b"\n")
        return "sha256:" + digest.hexdigest()

    def by_domain(self, domain: IdentityDomain) -> tuple[FingerprintDomainAssignment, ...]:
        return tuple(item for item in self.assignments if item.domain is domain)

    def domain_of(self, consumer_id: str) -> IdentityDomain:
        for item in self.assignments:
            if item.consumer_id == consumer_id:
                return item.domain
        raise FingerprintDomainError(f"no domain is assigned to {consumer_id!r}")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "assignments": [item.to_wire() for item in self.assignments],
            "fingerprint": self.fingerprint,
        }

    def to_public_dict(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        rules: dict[str, int] = {}
        for item in self.assignments:
            counts[item.domain.value] = counts.get(item.domain.value, 0) + 1
            rules[item.rule] = rules.get(item.rule, 0) + 1
        return {
            "schema": self.schema,
            "assignment_count": len(self.assignments),
            "domains": dict(sorted(counts.items())),
            "rules": dict(sorted(rules.items())),
            "fingerprint": self.fingerprint,
        }


def build_fingerprint_domain_assignments(
    assignments: tuple[FingerprintDomainAssignment, ...],
) -> FingerprintDomainAssignmentSet:
    """Sort and validate one complete assignment set; a caller never has to pre-sort."""

    if not isinstance(assignments, tuple):
        raise FingerprintDomainError("assignments must be a tuple")
    if not all(isinstance(item, FingerprintDomainAssignment) for item in assignments):
        raise FingerprintDomainError("assignments contain an invalid value")
    return FingerprintDomainAssignmentSet(
        assignments=tuple(sorted(assignments, key=lambda item: item.sort_key))
    )


__all__ = [
    "CURRENT_FINGERPRINT_DOMAIN_VERSION",
    "FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA",
    "FINGERPRINT_DOMAIN_SCHEMA",
    "MAX_ASSIGNMENTS",
    "MAX_FINGERPRINT_DOMAIN_VERSION",
    "NON_SEMANTIC_DOMAINS",
    "DomainFingerprint",
    "FingerprintConsumerKind",
    "IdentityDomain",
    "FingerprintDomainAssignment",
    "FingerprintDomainAssignmentSet",
    "FingerprintDomainError",
    "build_fingerprint_domain_assignments",
    "domain_fingerprint",
    "domain_label",
    "require_semantic_identity",
]
