"""M18-02 fingerprint domain separation and blast-radius tests.

The point of a domain is not that it is written down; it is that a value carrying the wrong one
cannot be used. So the tests that matter here are refusals: a release-integrity digest offered where
a semantic identity is required, a consumer assigned two domains, an assignment made by no rule at
all, and a record carrying something that looks like a path or a credential.

The rest fix the properties that make the shipped record reviewable -- one order, one fingerprint,
bytes that match a fresh generation, and every inventoried contract covered exactly once.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from comfyui_h3_context.core.contract_inventory import (
    CONTRACT_INVENTORY_SCHEMA,
    FORBIDDEN_RECORD_TEXT,
)
from comfyui_h3_context.core.fingerprint_domain import (
    CURRENT_FINGERPRINT_DOMAIN_VERSION,
    FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA,
    DomainFingerprint,
    FingerprintConsumerKind,
    FingerprintDomainAssignment,
    FingerprintDomainError,
    IdentityDomain,
    build_fingerprint_domain_assignments,
    domain_fingerprint,
    domain_label,
    require_semantic_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "fingerprint_domain_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "fingerprint_domain_v1.schema.json"
INVENTORY = REPO_ROOT / "comfyui_h3_context" / "contracts" / "contract_inventory_v1.json"


def _load_generator() -> Any:
    """Import the generator by path; `scripts/` is a directory, not an importable package."""

    name = "m18_02_fingerprint_domain_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "fingerprint_domain.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()


def _assignment(**overrides: object) -> FingerprintDomainAssignment:
    """A minimal well-formed row; each test names only the facet it is about."""

    base: dict[str, object] = {
        "consumer_id": "h3.example.wire.v1",
        "kind": FingerprintConsumerKind.CONTRACT,
        "domain": IdentityDomain.SEMANTIC,
        "domain_version": CURRENT_FINGERPRINT_DOMAIN_VERSION,
        "authority_paths": ("comfyui_h3_context/core/example.py",),
        "rule": "typed_runtime_wire_is_semantic",
        "evidence": "a typed runtime wire carries values that decide execution",
    }
    base.update(overrides)
    return FingerprintDomainAssignment(**base)  # type: ignore[arg-type]


class DomainFramingTests(unittest.TestCase):
    def test_the_domain_is_hashed_not_appended(self) -> None:
        """The same value under two domains must not produce one digest with two labels."""

        value = {"prompt_revision": 4, "task_mode": "t2va"}
        digests = {
            domain.value: domain_fingerprint(domain, value).digest for domain in IdentityDomain
        }
        self.assertEqual(len(set(digests.values())), len(IdentityDomain))

    def test_domain_version_is_part_of_the_identity(self) -> None:
        value = {"prompt_revision": 4}
        first = domain_fingerprint(IdentityDomain.SEMANTIC, value, version=1)
        second = domain_fingerprint(IdentityDomain.SEMANTIC, value, version=2)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(first.label, "semantic/v1")
        self.assertEqual(second.label, "semantic/v2")

    def test_a_domained_identity_reports_its_own_domain(self) -> None:
        identity = domain_fingerprint(IdentityDomain.PRESENTATION, {"revision": 2})
        self.assertIs(identity.domain, IdentityDomain.PRESENTATION)
        self.assertEqual(identity.to_wire()["domain"], "presentation")
        self.assertEqual(domain_label(IdentityDomain.PRESENTATION), "presentation/v1")

    def test_unsupported_domain_version_and_malformed_digest_fail_closed(self) -> None:
        with self.assertRaises(FingerprintDomainError):
            domain_fingerprint(IdentityDomain.SEMANTIC, {"a": 1}, version=0)
        with self.assertRaises(FingerprintDomainError):
            domain_fingerprint(IdentityDomain.SEMANTIC, {"a": 1}, version=100)
        with self.assertRaises(FingerprintDomainError):
            DomainFingerprint(IdentityDomain.SEMANTIC, 1, "sha256:not-a-digest")
        with self.assertRaises(FingerprintDomainError):
            domain_fingerprint("semantic", {"a": 1})  # type: ignore[arg-type]


class BlastRadiusTests(unittest.TestCase):
    """One mutation per domain that must change the identity, and one that must not."""

    REQUIRED = ({"a": 1, "b": 2}, {"a": 1, "b": 3})
    #: Key order is not a mutation: the canonicalizer sorts, so re-serialising a projection in a
    #: different order must never invalidate an artifact that depends on its identity.
    FORBIDDEN = ({"a": 1, "b": 2}, {"b": 2, "a": 1})

    def test_a_required_mutation_changes_every_domain(self) -> None:
        before, after = self.REQUIRED
        for domain in IdentityDomain:
            with self.subTest(domain=domain.value):
                self.assertNotEqual(
                    domain_fingerprint(domain, before).digest,
                    domain_fingerprint(domain, after).digest,
                )

    def test_a_forbidden_mutation_changes_no_domain(self) -> None:
        before, after = self.FORBIDDEN
        for domain in IdentityDomain:
            with self.subTest(domain=domain.value):
                self.assertEqual(
                    domain_fingerprint(domain, before).digest,
                    domain_fingerprint(domain, after).digest,
                )

    def test_one_domains_mutation_does_not_move_another_domain(self) -> None:
        """Domains are independent: changing what presentation measures cannot move semantics."""

        semantic = domain_fingerprint(IdentityDomain.SEMANTIC, {"prompt_revision": 4})
        presentation_before = domain_fingerprint(IdentityDomain.PRESENTATION, {"label": "a"})
        presentation_after = domain_fingerprint(IdentityDomain.PRESENTATION, {"label": "b"})
        self.assertNotEqual(presentation_before.digest, presentation_after.digest)
        self.assertEqual(
            semantic.digest,
            domain_fingerprint(IdentityDomain.SEMANTIC, {"prompt_revision": 4}).digest,
        )


class SemanticConsumptionTests(unittest.TestCase):
    """AC-M18-02-03: a release-integrity hash stays complete, and is refused as a semantic one."""

    def test_a_semantic_identity_is_returned(self) -> None:
        identity = domain_fingerprint(IdentityDomain.SEMANTIC, {"prompt_revision": 4})
        self.assertEqual(require_semantic_identity(identity), identity.digest)

    def test_a_release_integrity_identity_is_refused_and_stays_complete(self) -> None:
        identity = domain_fingerprint(IdentityDomain.RELEASE_INTEGRITY, {"bundle_bytes": 560106})
        with self.assertRaises(FingerprintDomainError) as caught:
            require_semantic_identity(identity, purpose="a cache identity")
        self.assertIn("release_integrity", str(caught.exception))
        self.assertIn("a cache identity", str(caught.exception))
        # Refused for one use, not damaged: the digest it carries is still the release identity.
        self.assertTrue(identity.digest.startswith("sha256:"))
        self.assertEqual(len(identity.digest), len("sha256:") + 64)

    def test_every_non_semantic_domain_is_refused(self) -> None:
        for domain in IdentityDomain:
            if domain is IdentityDomain.SEMANTIC:
                continue
            with self.subTest(domain=domain.value):
                with self.assertRaises(FingerprintDomainError):
                    require_semantic_identity(domain_fingerprint(domain, {"a": 1}))

    def test_a_bare_digest_string_cannot_be_consumed_at_all(self) -> None:
        """The whole mechanism depends on the domain travelling with the value."""

        with self.assertRaises(FingerprintDomainError):
            require_semantic_identity("sha256:" + "0" * 64)  # type: ignore[arg-type]


class AssignmentValidationTests(unittest.TestCase):
    def test_a_consumer_cannot_hold_two_domains(self) -> None:
        first = _assignment(domain=IdentityDomain.SEMANTIC)
        second = _assignment(domain=IdentityDomain.PRESENTATION)
        with self.assertRaises(FingerprintDomainError) as caught:
            build_fingerprint_domain_assignments((first, second))
        self.assertIn("more than one domain", str(caught.exception))

    def test_a_rule_name_must_be_a_rule_name(self) -> None:
        with self.assertRaises(FingerprintDomainError):
            _assignment(rule="Because I Said So")

    def test_a_record_refuses_a_private_path_or_a_credential(self) -> None:
        """`FORBIDDEN_RECORD_TEXT` is anchored, so these are values that *are* the unsafe thing."""

        for unsafe in (
            "C:/Users/someone/private.txt",
            "//host/share/private.txt",
            "https://example.invalid/signed",
            "authorization: Bearer abc",
            "api_key=abc",
        ):
            with self.subTest(evidence=unsafe):
                with self.assertRaises(FingerprintDomainError):
                    _assignment(evidence=unsafe)
        with self.assertRaises(FingerprintDomainError):
            _assignment(authority_paths=("/etc/passwd",))
        with self.assertRaises(FingerprintDomainError):
            _assignment(authority_paths=("comfyui_h3_context/../../private.py",))
        with self.assertRaises(FingerprintDomainError):
            _assignment(evidence="a line\nbreak is a control character")

    def test_the_forbidden_text_rule_has_exactly_one_authority(self) -> None:
        """A second copy of this regex would drift the first time either side was tightened."""

        from comfyui_h3_context.core import fingerprint_domain

        # `vars()` rather than attribute access: the constant is imported by that module, not
        # re-exported by it, and asking for the binding is the whole point of the assertion.
        self.assertIs(vars(fingerprint_domain)["FORBIDDEN_RECORD_TEXT"], FORBIDDEN_RECORD_TEXT)

    def test_authority_paths_are_bounded_sorted_and_unique(self) -> None:
        with self.assertRaises(FingerprintDomainError):
            _assignment(authority_paths=())
        with self.assertRaises(FingerprintDomainError):
            _assignment(authority_paths=("b/second.py", "a/first.py"))
        with self.assertRaises(FingerprintDomainError):
            _assignment(authority_paths=("a/first.py", "a/first.py"))

    def test_a_cross_language_partner_is_a_repository_path_or_absent(self) -> None:
        partnered = _assignment(cross_language_partner="frontend/src/contracts/example.ts")
        self.assertEqual(
            partnered.to_wire()["cross_language_partner"], "frontend/src/contracts/example.ts"
        )
        self.assertNotIn("cross_language_partner", _assignment().to_wire())
        with self.assertRaises(FingerprintDomainError):
            _assignment(cross_language_partner="https://example.invalid/module.ts")

    def test_the_set_is_sorted_and_addressable(self) -> None:
        assigned = build_fingerprint_domain_assignments(
            (
                _assignment(consumer_id="h3.zulu.v1"),
                _assignment(consumer_id="h3.alpha.v1", domain=IdentityDomain.FIXTURE),
            )
        )
        self.assertEqual(
            [item.consumer_id for item in assigned.assignments], ["h3.alpha.v1", "h3.zulu.v1"]
        )
        self.assertIs(assigned.domain_of("h3.alpha.v1"), IdentityDomain.FIXTURE)
        with self.assertRaises(FingerprintDomainError):
            assigned.domain_of("h3.nothing.v1")

    def test_the_set_fingerprint_moves_with_any_row(self) -> None:
        original = build_fingerprint_domain_assignments((_assignment(),))
        moved = build_fingerprint_domain_assignments(
            (replace(original.assignments[0], domain=IdentityDomain.FIXTURE),)
        )
        self.assertNotEqual(original.fingerprint, moved.fingerprint)


class ShippedArtifactTests(unittest.TestCase):
    """AC-M18-02-01: exactly one versioned domain per inventoried consumer, by a named rule."""

    document: dict[str, Any]
    rows: list[dict[str, Any]]
    inventory: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.rows = cls.document["assignments"]
        cls.inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    def test_the_artifact_matches_a_fresh_generation_byte_for_byte(self) -> None:
        expected = GENERATOR.artifact_bytes(GENERATOR.build_assignments())
        self.assertEqual(ARTIFACT.read_bytes(), expected)

    def test_the_artifact_declares_its_schema_and_a_folded_fingerprint(self) -> None:
        self.assertEqual(self.document["schema"], FINGERPRINT_DOMAIN_ASSIGNMENT_SCHEMA)
        self.assertEqual(self.document["fingerprint"], GENERATOR.build_assignments().fingerprint)

    def test_every_inventoried_contract_is_assigned_exactly_once(self) -> None:
        self.assertEqual(self.inventory["schema"], CONTRACT_INVENTORY_SCHEMA)
        inventoried = {entry["contract_id"] for entry in self.inventory["entries"]}
        assigned = [row["consumer_id"] for row in self.rows if row["kind"] == "contract"]
        self.assertEqual(set(assigned), inventoried)
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_every_row_names_the_rule_that_assigned_it(self) -> None:
        known = {rule.name for rule in GENERATOR.RULES} | {
            "declared_contract_domain",
            "declared_identity_site",
        }
        for row in self.rows:
            with self.subTest(consumer=row["consumer_id"]):
                self.assertIn(row["rule"], known)
                self.assertEqual(row["domain_version"], CURRENT_FINGERPRINT_DOMAIN_VERSION)

    def test_the_two_gate_named_consumers_are_assigned_explicitly(self) -> None:
        """Neither may arrive at its domain by omission; both are declared identity sites."""

        rows = {row["consumer_id"]: row for row in self.rows}

        app_mode = rows["frontend/appMode.graph_projection_identity"]
        self.assertEqual(app_mode["domain"], "presentation")
        self.assertEqual(app_mode["rule"], "declared_identity_site")
        # Browser-local by construction: JSON.stringify over an unsorted object has no Python twin.
        self.assertNotIn("cross_language_partner", app_mode)

        pinned = rows["core/generation_profile.pinned_template_revision"]
        self.assertEqual(pinned["domain"], "release_integrity")
        self.assertEqual(pinned["rule"], "declared_identity_site")

        agreement = rows["frontend/sidebarHost.prompt_fingerprint_agreement"]
        self.assertEqual(agreement["domain"], "semantic")
        self.assertEqual(
            agreement["cross_language_partner"],
            "frontend/src/contracts/canonicalFingerprint.ts",
        )

    def test_the_record_carries_no_content_path_or_credential(self) -> None:
        for row in self.rows:
            for field_name in ("consumer_id", "rule", "evidence"):
                value = row[field_name]
                with self.subTest(consumer=row["consumer_id"], field=field_name):
                    if field_name == "consumer_id" and value.startswith(
                        ("comfyui-h3-context://", "h3-context://", "https://")
                    ):
                        continue  # a repository-owned `$id`, admitted by namespace
                    self.assertIsNone(FORBIDDEN_RECORD_TEXT.search(value))
            for path in row["authority_paths"]:
                with self.subTest(consumer=row["consumer_id"], path=path):
                    self.assertFalse(path.startswith("/"))
                    self.assertNotIn("..", path.split("/"))

    def test_the_artifact_validates_against_its_schema(self) -> None:
        jsonschema = __import__("jsonschema")
        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.validate(self.document, schema)


class GeneratorRefusalTests(unittest.TestCase):
    def test_a_consumer_matching_no_rule_fails_generation(self) -> None:
        """There is no catch-all, so an unclassifiable row stops the build (AC-M18-02-01)."""

        row = GENERATOR._Row(
            contract_id="h3.unclassifiable.v1",
            kinds=frozenset({"something_new"}),
            boundaries=frozenset({"process_local"}),
            authority_paths=("comfyui_h3_context/core/example.py",),
        )
        with self.assertRaises(GENERATOR.DomainAssignmentError) as caught:
            GENERATOR.assign(row)
        self.assertIn("add a rule rather than a default", str(caught.exception))

    def test_a_declaration_naming_an_uninventoried_contract_fails(self) -> None:
        original = GENERATOR.DECLARED
        try:
            GENERATOR.DECLARED = {
                **original,
                "h3.not.in.the.inventory.v1": GENERATOR._Declared(
                    IdentityDomain.FIXTURE, "a declaration with nothing to declare about"
                ),
            }
            with self.assertRaises(GENERATOR.DomainAssignmentError):
                GENERATOR.build_assignments()
        finally:
            GENERATOR.DECLARED = original

    def test_an_inventory_that_is_not_the_inventory_is_refused(self) -> None:
        original = GENERATOR.INVENTORY_PATH
        try:
            GENERATOR.INVENTORY_PATH = Path("governance/contracts/sbom.spdx.json")
            with self.assertRaises(GENERATOR.DomainAssignmentError) as caught:
                GENERATOR.read_inventory()
            self.assertIn("expected schema", str(caught.exception))
        finally:
            GENERATOR.INVENTORY_PATH = original


if __name__ == "__main__":
    unittest.main()
