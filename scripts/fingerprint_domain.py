"""Generate and verify the M18-02 fingerprint-domain assignment over the M18-01 inventory.

Every fingerprint consumer names exactly one versioned domain (AC-M18-02-01).  The assignment is a
*record*: it does not recompute, reframe or migrate a single existing fingerprint.  Threading a
domain tag through the repository's 289 fingerprint sites would change every fingerprint value it
touched, which the M18-02 plan prohibits because it invalidates accepted artifacts, caches and
receipts.  So existing identities gain a recorded domain here, and only identities introduced from
M18-02 onward are produced through ``core.fingerprint_domain.domain_fingerprint``.

Two rules run through everything.

Every assignment is made by a **named rule with a stated criterion**, recorded on the row.  A
consumer that matches no rule fails generation rather than defaulting into a domain -- there is no
catch-all, and in particular nothing falls into ``release_integrity`` by omission.

A contract and an identity are **not the same unit**.  Some contracts carry more than one kind of
identity: the sidebar workspace projection is presentation, but the prompt fingerprint inside it is
the one cross-language semantic identity in the repository; the generation profile is semantic, but
the pinned upstream template revision beside it is release integrity.  Forcing one domain onto the
whole contract would make the record say something false, so those identities are assigned as
declared identity sites in their own right.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.fingerprint_domain import (  # noqa: E402
    CURRENT_FINGERPRINT_DOMAIN_VERSION,
    FingerprintConsumerKind,
    FingerprintDomainAssignment,
    FingerprintDomainAssignmentSet,
    FingerprintDomainError,
    IdentityDomain,
    build_fingerprint_domain_assignments,
)

INVENTORY_PATH = Path("comfyui_h3_context/contracts/contract_inventory_v1.json")
ARTIFACT_PATH = Path("governance/contracts/fingerprint_domain_v1.json")
INVENTORY_SCHEMA = "h3-context-contract-inventory/1"


class DomainAssignmentError(RuntimeError):
    """Raised when the inventory cannot be read, or a consumer matches no rule."""


@dataclass(frozen=True)
class _Row:
    """One contract identity, and the facets of it the assignment rules may look at.

    An identity can be inventoried more than once -- `h3.compatibility.matrix.v1` is both the JSON
    document that carries it and the Python constant that declares it -- but it is still one
    identity, so the rows are merged before classification.  Classifying the copies separately would
    invite the two halves of a single identity into two different domains, which is the exact
    confusion this item exists to prevent.
    """

    contract_id: str
    kinds: frozenset[str]
    boundaries: frozenset[str]
    authority_paths: tuple[str, ...]

    @property
    def haystack(self) -> str:
        return " ".join((self.contract_id, *self.authority_paths)).casefold()


@dataclass(frozen=True)
class _Rule:
    """One classification rung: a name, the domain it assigns, and why that follows."""

    name: str
    domain: IdentityDomain
    evidence: str
    matches: Callable[[_Row], bool]


@dataclass(frozen=True)
class _Declared:
    """A domain decided by declaration because the mechanical rule would be wrong."""

    domain: IdentityDomain
    evidence: str


@dataclass(frozen=True)
class _Site:
    """An identity that is not a property of any one contract, assigned in its own right."""

    consumer_id: str
    domain: IdentityDomain
    authority_paths: tuple[str, ...]
    evidence: str
    cross_language_partner: str | None = None


#: Supply chain, packaging and pinned upstream material.  Broad on purpose: assigning
#: `release_integrity` only ever *restricts* what an identity may be consumed as, so a generous list
#: is the safe direction to be wrong in.
_RELEASE_INTEGRITY_MARKERS = (
    "supply_chain",
    "supply-chain",
    "sbom",
    "license",
    "installation_profiles",
    "installation-profiles",
    "compatibility_matrix",
    "compatibility.matrix",
    "public.manifest",
    "public_manifest",
    "source_drift",
    "source_ledger",
    "source_requalification",
    "host.canary",
    "host_canary",
)
#: Projection and stale-render material.  Every one of these answers "is what the browser is showing
#: still the current render", which is never a licence to reuse a generated artifact.
_PRESENTATION_MARKERS = (
    "ui.projection",
    "ui_projection",
    "sidebar",
    "production_workbench",
    "product.shell",
    "product_shell",
    "transaction_transparency",
    "media_preview",
    "preview",
    "projection",
)

#: Assignments the shape rules cannot make correctly, each with the reason it is declared.
DECLARED: Mapping[str, _Declared] = {
    "h3-context-canonical/1": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "the canonicalization profile frames the bytes an identity is computed over; it is "
        "structure, never an instance value",
    ),
    "h3-context-contract-inventory/1": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "the inventory describes the shape of the contract surface and authorizes no execution",
    ),
    "h3-context-fingerprint-domain/1": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "the framing envelope a domained identity is computed inside; it is the frame, not the "
        "identity it frames",
    ),
    "h3-context-fingerprint-domain-assignment/1": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "this record describes which domain each identity belongs to; it is not itself one",
    ),
    "h3-node-contract/1": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "the node contract registry declares socket shape, not the values crossing a socket",
    ),
    "h3.context.contract.v2": _Declared(
        IdentityDomain.WIRE_CONTRACT,
        "the v2 envelope is version framing around a payload it does not interpret",
    ),
    "h3.context.package_compatibility_policy.v1": _Declared(
        IdentityDomain.RELEASE_INTEGRITY,
        "the package compatibility registry governs release versioning and removal policy; it "
        "authorizes no runtime execution, cache reuse or receipt",
    ),
    "h3-context-subgraph-fixture/1": _Declared(
        IdentityDomain.FIXTURE,
        "a packaged subgraph fixture identity; it never authorizes reuse of a generated artifact",
    ),
    "h3-context-workflow-surface-matrix/1": _Declared(
        IdentityDomain.FIXTURE,
        "the declared workflow-surface matrix identifies packaged fixtures, not runtime values",
    ),
}

#: Identities assigned in their own right.  The first two are the consumers the M18-02 activation
#: gate names explicitly, and neither is allowed to arrive at its domain by omission.
IDENTITY_SITES: tuple[_Site, ...] = (
    _Site(
        consumer_id="frontend/appMode.graph_projection_identity",
        domain=IdentityDomain.PRESENTATION,
        # M23-28: `graphFingerprint` lives in the App Mode contract module;
        # `appMode.ts` remains the public door.
        authority_paths=("frontend/src/host/appModeContract.ts",),
        evidence=(
            "browser-local remount and stale-render identity over JSON.stringify of a graph "
            "object: insertion order, no key sort, no depth or item ceiling, so it is not "
            "canonical and has no Python partner to be compared against"
        ),
        cross_language_partner=None,
    ),
    _Site(
        consumer_id="frontend/sidebarHost.prompt_fingerprint_agreement",
        domain=IdentityDomain.SEMANTIC,
        authority_paths=("frontend/src/host/sidebarHost.ts",),
        evidence=(
            "the only genuine cross-language identity: canonical prompt text fingerprinted in the "
            "browser and compared against the backend-produced prompt fingerprint"
        ),
        cross_language_partner="frontend/src/contracts/canonicalFingerprint.ts",
    ),
    _Site(
        consumer_id="core/generation_profile.pinned_template_revision",
        domain=IdentityDomain.RELEASE_INTEGRITY,
        authority_paths=("comfyui_h3_context/core/generation_profile.py",),
        evidence=(
            "the pinned upstream workflow-template revision and its per-template digests; a pinned "
            "upstream artifact, and never the identity that decides what a run produces"
        ),
        cross_language_partner=None,
    ),
)

#: Ordered.  The first rung whose criterion holds decides, and the rung's name is recorded on the
#: row so the classification can be checked against its criterion rather than against an opinion.
#:
#: The order encodes one deliberate decision: **kind separates structure from instance before any
#: marker is consulted.**  A `.schema.json` file and a codec declare the shape of a contract, which
#: the frozen matrix calls `wire_contract` whatever that contract happens to carry; the markers then
#: classify only the identities that carry instance values.  Consulting markers first produced a
#: record that could not be defended: `sidebarWorkspaceCodec` landed in `presentation` and
#: `productionWorkbenchCodec` in `wire_contract`, two codecs on the same boundary separated by
#: nothing but whether a marker happened to be spelled with an underscore.
RULES: tuple[_Rule, ...] = (
    _Rule(
        name="package_metadata_is_release_integrity",
        domain=IdentityDomain.RELEASE_INTEGRITY,
        evidence="packaging metadata decides what is published, never what a run produces",
        matches=lambda row: "package_metadata" in row.kinds,
    ),
    _Rule(
        name="pinned_host_contract_is_release_integrity",
        domain=IdentityDomain.RELEASE_INTEGRITY,
        evidence="a host contract pinned at a host version is a pinned upstream revision",
        matches=lambda row: "host_node" in row.kinds or "host_core" in row.boundaries,
    ),
    _Rule(
        name="json_schema_is_wire_contract",
        domain=IdentityDomain.WIRE_CONTRACT,
        evidence="a JSON Schema is the structural shape of a contract, not an instance of it",
        matches=lambda row: "json_schema" in row.kinds,
    ),
    _Rule(
        name="frontend_codec_is_wire_contract",
        domain=IdentityDomain.WIRE_CONTRACT,
        evidence="a codec declares the structural shape crossing the language boundary",
        matches=lambda row: "frontend_codec" in row.kinds,
    ),
    _Rule(
        name="supply_chain_material_is_release_integrity",
        domain=IdentityDomain.RELEASE_INTEGRITY,
        evidence="supply-chain, packaging and pinned-upstream material by declared marker",
        matches=lambda row: any(marker in row.haystack for marker in _RELEASE_INTEGRITY_MARKERS),
    ),
    _Rule(
        name="workflow_fixture_is_fixture",
        domain=IdentityDomain.FIXTURE,
        evidence="a packaged workflow fixture identity never authorizes runtime reuse",
        matches=lambda row: "workflow_fixture" in row.kinds,
    ),
    _Rule(
        name="test_only_boundary_is_fixture",
        domain=IdentityDomain.FIXTURE,
        evidence="a contract only tests declare is evidence about a test, never about a runtime",
        matches=lambda row: "test_only" in row.boundaries,
    ),
    _Rule(
        name="projection_material_is_presentation",
        domain=IdentityDomain.PRESENTATION,
        evidence="projection and stale-render material by declared marker; never a cache identity",
        matches=lambda row: any(marker in row.haystack for marker in _PRESENTATION_MARKERS),
    ),
    _Rule(
        name="typed_runtime_wire_is_semantic",
        domain=IdentityDomain.SEMANTIC,
        evidence="a typed runtime wire carries the instance values that decide execution, cache "
        "reuse or a receipt",
        matches=lambda row: bool(row.kinds & {"python_wire", "json_data"}),
    ),
)


def read_inventory() -> tuple[_Row, ...]:
    """Read the M18-01 artifact as data, refusing anything that is not that artifact."""

    path = ROOT / INVENTORY_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DomainAssignmentError(f"contract inventory is unreadable: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != INVENTORY_SCHEMA:
        raise DomainAssignmentError("contract inventory does not declare the expected schema")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DomainAssignmentError("contract inventory carries no entries")
    merged: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise DomainAssignmentError("contract inventory entry is not an object")
        paths = entry.get("authority_paths")
        if not isinstance(paths, list) or not paths:
            raise DomainAssignmentError("contract inventory entry declares no authority")
        record = merged.setdefault(
            str(entry["contract_id"]), {"kinds": set(), "boundaries": set(), "paths": set()}
        )
        record["kinds"].add(str(entry["kind"]))
        record["boundaries"].add(str(entry["boundary"]))
        record["paths"].update(str(item) for item in paths)
    return tuple(
        _Row(
            contract_id=contract_id,
            kinds=frozenset(record["kinds"]),
            boundaries=frozenset(record["boundaries"]),
            authority_paths=tuple(sorted(record["paths"])),
        )
        for contract_id, record in sorted(merged.items())
    )


def assign(row: _Row) -> FingerprintDomainAssignment:
    """Assign exactly one domain, by declaration or by the first rung that holds."""

    declared = DECLARED.get(row.contract_id)
    if declared is not None:
        return FingerprintDomainAssignment(
            consumer_id=row.contract_id,
            kind=FingerprintConsumerKind.CONTRACT,
            domain=declared.domain,
            domain_version=CURRENT_FINGERPRINT_DOMAIN_VERSION,
            authority_paths=tuple(sorted(row.authority_paths)),
            rule="declared_contract_domain",
            evidence=declared.evidence,
        )
    for rule in RULES:
        if rule.matches(row):
            return FingerprintDomainAssignment(
                consumer_id=row.contract_id,
                kind=FingerprintConsumerKind.CONTRACT,
                domain=rule.domain,
                domain_version=CURRENT_FINGERPRINT_DOMAIN_VERSION,
                authority_paths=tuple(sorted(row.authority_paths)),
                rule=rule.name,
                evidence=rule.evidence,
            )
    raise DomainAssignmentError(
        f"no rule assigns a domain to {row.contract_id!r}; add a rule rather than a default"
    )


def build_assignments() -> FingerprintDomainAssignmentSet:
    """Assign every inventoried contract, then every declared identity site."""

    rows = read_inventory()
    assignments = [assign(row) for row in rows]
    for site in IDENTITY_SITES:
        assignments.append(
            FingerprintDomainAssignment(
                consumer_id=site.consumer_id,
                kind=FingerprintConsumerKind.IDENTITY_SITE,
                domain=site.domain,
                domain_version=CURRENT_FINGERPRINT_DOMAIN_VERSION,
                authority_paths=tuple(sorted(site.authority_paths)),
                rule="declared_identity_site",
                evidence=site.evidence,
                cross_language_partner=site.cross_language_partner,
            )
        )
    unknown = sorted(set(DECLARED) - {row.contract_id for row in rows})
    if unknown:
        raise DomainAssignmentError(f"declaration names an uninventoried contract: {unknown[0]}")
    return build_fingerprint_domain_assignments(tuple(assignments))


def artifact_bytes(assigned: FingerprintDomainAssignmentSet) -> bytes:
    return (
        json.dumps(assigned.to_wire(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        assigned = build_assignments()
    except (DomainAssignmentError, FingerprintDomainError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(assigned)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check:
        current = target.read_bytes() if target.is_file() else b""
        if current != expected:
            print(
                json.dumps(
                    {"status": "FAIL", "detail": "fingerprint domain artifact is stale"},
                    ensure_ascii=False,
                )
            )
            return 1
    print(json.dumps({"status": "PASS", **assigned.to_public_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
