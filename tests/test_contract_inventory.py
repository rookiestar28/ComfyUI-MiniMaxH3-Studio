"""M18-01 contract-surface inventory tests.

The inventory's whole value is that it refuses to overclaim. A grep that finds nothing is not
evidence a contract is unused; two modules declaring one identity is not a licence to pick an
owner; a fixture that constructs a value is not a runtime that reaches it. Every test below fixes
one of those refusals, and the last few fix the properties that make the shipped artifact reviewable
at all -- one order, one fingerprint, and bytes that match a fresh generation.

Nothing here asserts on English prose, and no fixture carries a private path, a credential or a
byte of real content.
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
    KNOWN_IDENTITY_NAMESPACES,
    MAX_INVENTORY_ENTRIES,
    PROTECTED_BOUNDARIES,
    CompatibilityDisposition,
    ContractBoundary,
    ContractDisposition,
    ContractEntry,
    ContractInventoryError,
    ContractKind,
    InventoryBlocker,
    PersistenceClass,
    PublicationStatus,
    build_contract_inventory,
)
from comfyui_h3_context.core.reachability import ReachabilityDisposition, ReachabilitySource

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "comfyui_h3_context" / "contracts" / "contract_inventory_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "contract_inventory_v1.schema.json"


def _load_generator() -> Any:
    """Import the generator by path; `scripts/` is a directory, not an importable package.

    The module is registered in `sys.modules` before execution because `@dataclass` resolves its
    defining module by name while the class body is being processed.
    """

    name = "m18_01_contract_inventory_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "contract_inventory.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()


def _entry(**overrides: object) -> ContractEntry:
    """A minimal well-formed entry; each test names only the facet it is about."""

    base: dict[str, object] = {
        "contract_id": "h3.example.wire.v1",
        "kind": ContractKind.PYTHON_WIRE,
        "authority_paths": ("comfyui_h3_context/core/example.py",),
        "dialect": "not_applicable",
        "producers": ("comfyui_h3_context/core/example.py",),
        "consumers": ("comfyui_h3_context/core/reader.py",),
        "boundary": ContractBoundary.PROCESS_LOCAL,
        "persistence": PersistenceClass.TRANSIENT,
        "publication": PublicationStatus.PACKAGED,
        "reachability": None,
        "reachability_source": None,
        "fingerprint_domain": "example",
        "disposition": ContractDisposition.KEEP,
        "compatibility": CompatibilityDisposition.OLD_READERS_UNAFFECTED,
        "evidence": "one authority, one reader",
    }
    base.update(overrides)
    return ContractEntry(**base)  # type: ignore[arg-type]


class BlockedClaimTests(unittest.TestCase):
    """A recorded obstruction and a confident disposition cannot coexist."""

    def test_duplicate_identity_is_a_blocker_and_no_owner_is_inferred(self) -> None:
        for disposition in (
            ContractDisposition.KEEP,
            ContractDisposition.SIMPLIFY,
            ContractDisposition.DEPRECATE,
        ):
            with self.subTest(disposition=disposition):
                with self.assertRaises(ContractInventoryError):
                    _entry(
                        blockers=(InventoryBlocker.DUPLICATE_SCHEMA_IDENTITY,),
                        disposition=disposition,
                    )

    def test_duplicate_identity_may_only_be_recorded_honestly(self) -> None:
        for disposition in (
            ContractDisposition.NEED_EVIDENCE,
            ContractDisposition.BLOCKED_CORRECTION_REQUIRED,
        ):
            with self.subTest(disposition=disposition):
                entry = _entry(
                    blockers=(InventoryBlocker.DUPLICATE_SCHEMA_IDENTITY,),
                    disposition=disposition,
                )
                self.assertIs(entry.disposition, disposition)

    def test_an_authority_with_no_discoverable_owner_fails_closed(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(
                blockers=(InventoryBlocker.UNRESOLVED_OWNER,),
                disposition=ContractDisposition.KEEP,
            )

    def test_an_unknown_consumer_can_never_become_a_retirement_candidate(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(
                blockers=(InventoryBlocker.UNKNOWN_CONSUMER,),
                boundary=ContractBoundary.PROCESS_LOCAL,
                consumers=(),
                disposition=ContractDisposition.RETIRE_CANDIDATE,
            )

    def test_blockers_are_sorted_and_unique(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(
                blockers=(
                    InventoryBlocker.UNRESOLVED_OWNER,
                    InventoryBlocker.DUPLICATE_SCHEMA_IDENTITY,
                ),
                disposition=ContractDisposition.NEED_EVIDENCE,
            )
        with self.assertRaises(ContractInventoryError):
            _entry(
                blockers=(
                    InventoryBlocker.UNKNOWN_CONSUMER,
                    InventoryBlocker.UNKNOWN_CONSUMER,
                ),
                disposition=ContractDisposition.NEED_EVIDENCE,
            )


class PositionBindingBlockerTests(unittest.TestCase):
    """AC-M18-01-08: a reachable route with an incomplete binding cannot be waved through.

    The precondition does not currently hold -- M21-02 delivered the derived positional binding and
    the VLM route is recorded `unsupported` -- so no entry carries this blocker. The rule is still
    enforced here, because the value of a fail-closed rule is that it holds before it is needed.
    """

    def test_an_incomplete_binding_blocks_a_reachable_route(self) -> None:
        for disposition in (
            ContractDisposition.KEEP,
            ContractDisposition.SIMPLIFY,
            ContractDisposition.DEPRECATE,
            ContractDisposition.RETIRE_CANDIDATE,
        ):
            with self.subTest(disposition=disposition):
                with self.assertRaises(ContractInventoryError):
                    _entry(
                        boundary=ContractBoundary.UNTRUSTED_INPUT,
                        reachability=ReachabilityDisposition.REACHABLE,
                        reachability_source=ReachabilitySource.PUBLIC_NODE,
                        blockers=(InventoryBlocker.INCOMPLETE_POSITION_BINDING,),
                        disposition=disposition,
                    )

    def test_an_incomplete_binding_is_recordable_as_a_correction(self) -> None:
        entry = _entry(
            boundary=ContractBoundary.UNTRUSTED_INPUT,
            reachability=ReachabilityDisposition.REACHABLE,
            reachability_source=ReachabilitySource.PUBLIC_NODE,
            blockers=(InventoryBlocker.INCOMPLETE_POSITION_BINDING,),
            disposition=ContractDisposition.BLOCKED_CORRECTION_REQUIRED,
            compatibility=CompatibilityDisposition.MIGRATION_REQUIRED,
        )
        self.assertIs(entry.disposition, ContractDisposition.BLOCKED_CORRECTION_REQUIRED)


class AbsentEvidenceTests(unittest.TestCase):
    """The central rule: not finding a reader is not the same as there being none."""

    def test_no_discovered_consumer_must_fail_closed(self) -> None:
        for disposition in (
            ContractDisposition.KEEP,
            ContractDisposition.SIMPLIFY,
            ContractDisposition.DEPRECATE,
            ContractDisposition.RETIRE_CANDIDATE,
        ):
            with self.subTest(disposition=disposition):
                with self.assertRaises(ContractInventoryError):
                    _entry(
                        consumers=(),
                        boundary=ContractBoundary.PROCESS_LOCAL,
                        disposition=disposition,
                    )

    def test_no_discovered_consumer_is_recordable_as_need_evidence(self) -> None:
        entry = _entry(
            consumers=(),
            boundary=ContractBoundary.PROCESS_LOCAL,
            disposition=ContractDisposition.NEED_EVIDENCE,
            compatibility=CompatibilityDisposition.UNKNOWN,
            blockers=(InventoryBlocker.UNKNOWN_CONSUMER,),
        )
        self.assertEqual(entry.consumers, ())
        self.assertIs(entry.disposition, ContractDisposition.NEED_EVIDENCE)

    def test_a_deferred_note_cannot_create_an_edge_or_change_a_disposition(self) -> None:
        note = "a reader may exist in a downstream workflow this repository cannot scan"
        with self.assertRaises(ContractInventoryError):
            _entry(
                consumers=(),
                boundary=ContractBoundary.PROCESS_LOCAL,
                disposition=ContractDisposition.KEEP,
                deferred_consumer_note=note,
            )
        recorded = _entry(
            consumers=(),
            boundary=ContractBoundary.PROCESS_LOCAL,
            disposition=ContractDisposition.NEED_EVIDENCE,
            compatibility=CompatibilityDisposition.UNKNOWN,
            blockers=(InventoryBlocker.UNKNOWN_CONSUMER,),
            deferred_consumer_note=note,
        )
        # The note is prose beside the evidence, never evidence itself.
        self.assertEqual(recorded.consumers, ())
        self.assertEqual(recorded.producers, ("comfyui_h3_context/core/example.py",))
        self.assertIs(recorded.disposition, ContractDisposition.NEED_EVIDENCE)


class ProtectedBoundaryTests(unittest.TestCase):
    """A reader this repository does not control cannot be retired past."""

    def test_every_protected_boundary_refuses_direct_retirement(self) -> None:
        self.assertIn(ContractBoundary.HOST_CORE, PROTECTED_BOUNDARIES)
        for boundary in sorted(PROTECTED_BOUNDARIES, key=lambda item: item.value):
            with self.subTest(boundary=boundary):
                with self.assertRaises(ContractInventoryError):
                    _entry(boundary=boundary, disposition=ContractDisposition.RETIRE_CANDIDATE)

    def test_an_override_cannot_promote_past_the_rule(self) -> None:
        override = GENERATOR._Override(
            evidence="a declared decision may not outrank a validated invariant",
            boundary=ContractBoundary.PUBLIC,
            disposition=ContractDisposition.RETIRE_CANDIDATE,
        )
        with self.assertRaises(ContractInventoryError):
            _entry(boundary=override.boundary, disposition=override.disposition)

    def test_a_process_local_contract_may_be_a_retirement_candidate(self) -> None:
        entry = _entry(
            boundary=ContractBoundary.PROCESS_LOCAL,
            disposition=ContractDisposition.RETIRE_CANDIDATE,
            compatibility=CompatibilityDisposition.NO_READERS,
        )
        self.assertIs(entry.disposition, ContractDisposition.RETIRE_CANDIDATE)

    def test_a_host_contract_is_recorded_never_dispositioned(self) -> None:
        for disposition in (
            ContractDisposition.SIMPLIFY,
            ContractDisposition.DEPRECATE,
            ContractDisposition.BLOCKED_CORRECTION_REQUIRED,
        ):
            with self.subTest(disposition=disposition):
                with self.assertRaises(ContractInventoryError):
                    _entry(
                        boundary=ContractBoundary.HOST_CORE,
                        kind=ContractKind.HOST_NODE,
                        disposition=disposition,
                        blockers=(InventoryBlocker.UNKNOWN_CONSUMER,)
                        if disposition is ContractDisposition.BLOCKED_CORRECTION_REQUIRED
                        else (),
                    )


class ReachabilityClaimTests(unittest.TestCase):
    """The claim reuses `core/reachability.py`, including the rule a fixture cannot satisfy."""

    def test_an_injected_fixture_never_establishes_reachability(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(
                reachability=ReachabilityDisposition.REACHABLE,
                reachability_source=ReachabilitySource.INJECTED_FIXTURE,
            )

    def test_an_injected_fixture_records_injected_only(self) -> None:
        entry = _entry(
            reachability=ReachabilityDisposition.INJECTED_ONLY,
            reachability_source=ReachabilitySource.INJECTED_FIXTURE,
        )
        self.assertIs(entry.reachability, ReachabilityDisposition.INJECTED_ONLY)

    def test_a_reachable_contract_must_name_what_reaches_it(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(
                reachability=ReachabilityDisposition.REACHABLE,
                reachability_source=ReachabilitySource.NONE,
            )

    def test_a_host_contract_named_only_by_a_fixture_records_that_honestly(self) -> None:
        """Recording "only fixtures name it" is the honest result, not an overclaim to refuse."""

        entry = _entry(
            boundary=ContractBoundary.HOST_CORE,
            kind=ContractKind.HOST_NODE,
            reachability=ReachabilityDisposition.INJECTED_ONLY,
            reachability_source=ReachabilitySource.INJECTED_FIXTURE,
        )
        self.assertIs(entry.reachability, ReachabilityDisposition.INJECTED_ONLY)

    def test_a_claim_and_its_source_are_recorded_together_or_not_at_all(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(reachability=ReachabilityDisposition.UNSUPPORTED, reachability_source=None)
        with self.assertRaises(ContractInventoryError):
            _entry(reachability=None, reachability_source=ReachabilitySource.NONE)
        silent = _entry()
        self.assertIsNone(silent.reachability)
        self.assertIsNone(silent.reachability_source)
        self.assertNotIn("reachability", silent.to_wire())

    def test_an_internal_wire_makes_no_reachability_claim(self) -> None:
        """`unsupported` is a product statement, so it is not the default for a private wire."""

        candidate = GENERATOR._Candidate(
            contract_id="h3.example.wire.v1",
            kind=ContractKind.PYTHON_WIRE,
            authority_paths=("comfyui_h3_context/core/example.py",),
            dialect="not_applicable",
            needles=frozenset({"h3.example.wire.v1"}),
            fingerprint_domain="example",
        )
        self.assertFalse(
            GENERATOR._carries_a_route(candidate, {"comfyui_h3_context/core/reader.py"})
        )
        self.assertTrue(
            GENERATOR._carries_a_route(candidate, {"comfyui_h3_context/adapters/comfyui_vlm.py"})
        )


class UnsafeInputTests(unittest.TestCase):
    """Nothing shaped like a private path, a credential or an escape reaches the artifact."""

    def test_an_unsafe_path_is_refused(self) -> None:
        for unsafe in (
            "../outside/contract.py",
            "comfyui_h3_context/../../etc/passwd",
            "/absolute/contract.py",
            "C:/Users/private/contract.py",
            "\\\\server\\share\\contract.py",
            "comfyui_h3_context//double.py",
            "comfyui_h3_context/trailing/",
        ):
            with self.subTest(path=unsafe):
                with self.assertRaises(ContractInventoryError):
                    _entry(authority_paths=(unsafe,))

    def test_credential_shaped_text_is_refused(self) -> None:
        for unsafe in (
            "authorization: Bearer redacted",
            "api_key = redacted",
            "token=redacted",
            "signed_url for the asset",
            "https://example.com/private",
        ):
            with self.subTest(text=unsafe):
                with self.assertRaises(ContractInventoryError):
                    _entry(evidence=unsafe)
                with self.assertRaises(ContractInventoryError):
                    _entry(fingerprint_domain=unsafe)

    def test_a_repository_owned_identity_namespace_is_admitted(self) -> None:
        for namespace in KNOWN_IDENTITY_NAMESPACES:
            with self.subTest(namespace=namespace):
                entry = _entry(contract_id=f"{namespace}contracts/example_v1.schema.json")
                self.assertTrue(entry.contract_id.startswith(namespace))

    def test_a_foreign_identity_namespace_is_refused(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(contract_id="https://attacker.invalid/schemas/example.json")

    def test_paths_are_bounded_sorted_and_unique(self) -> None:
        with self.assertRaises(ContractInventoryError):
            _entry(consumers=("b/second.py", "a/first.py"))
        with self.assertRaises(ContractInventoryError):
            _entry(consumers=("a/first.py", "a/first.py"))
        with self.assertRaises(ContractInventoryError):
            _entry(authority_paths=())


class ScanBoundTests(unittest.TestCase):
    """Discovery is bounded and refuses a link rather than following it."""

    def test_the_scan_declares_bounds(self) -> None:
        self.assertLessEqual(GENERATOR.MAX_SCANNED_FILES, 8_192)
        self.assertLessEqual(GENERATOR.MAX_FILE_BYTES, 8 * 1024 * 1024)
        self.assertLessEqual(GENERATOR.MAX_SCANNED_BYTES, 64 * 1024 * 1024)

    def test_an_over_budget_scan_is_refused_not_truncated(self) -> None:
        original = GENERATOR.MAX_SCANNED_FILES
        try:
            GENERATOR.MAX_SCANNED_FILES = 1
            with self.assertRaises(GENERATOR.InventoryGenerationError):
                GENERATOR.scan_repository()
        finally:
            GENERATOR.MAX_SCANNED_FILES = original

    def test_the_inventory_never_supplies_its_own_evidence(self) -> None:
        """Every self-describing file is excluded, all three for the same reason.

        The artifact names every identity it records, and this test file names identities in order
        to assert about them. Indexing either would let the inventory manufacture evidence: a test
        asserting that a contract has no reader would itself become that contract's reader. The
        M18-02 fingerprint-domain record joins them because it assigns a domain to every identity in
        this inventory, so indexed it would be the sole discovered reader of every contract nothing
        else reads.

        The set is asserted exactly, not merely for membership. It is a bounded decision about what
        this measurement is allowed not to see, and it must not grow by accident.
        """

        scanned = set(GENERATOR.scan_repository())
        for excluded in GENERATOR.SELF_DESCRIBING_PATHS:
            with self.subTest(path=excluded):
                self.assertNotIn(excluded, scanned)
        self.assertEqual(
            GENERATOR.SELF_DESCRIBING_PATHS,
            frozenset(
                {
                    GENERATOR.ARTIFACT_PATH.as_posix(),
                    "tests/test_contract_inventory.py",
                    "governance/contracts/fingerprint_domain_v1.json",
                    # M18-05: the eligibility record names every schema it judges, so indexing it
                    # would let the record supply the very evidence it exists to weigh.
                    "governance/contracts/retirement_eligibility_v1.json",
                    # M23-45's ownership record, generator and regression name the contracts
                    # they describe; scanning them would manufacture runtime-reader evidence.
                    "governance/contracts/architecture_fitness_v1.json",
                    "scripts/architecture_fitness.py",
                    "tests/test_architecture_fitness.py",
                    # M23-48: the readership record lists every packaged artifact by path in order
                    # to say who reads it, and its test pins two of those classes by path. Indexed,
                    # the record alone would give every contract in this inventory a reader -- the
                    # broadest instance of the same defect, and the one that would quietly empty
                    # the no-reader population the retirement rule depends on.
                    "governance/contracts/packaged_artifact_readership_v1.json",
                    "tests/test_m23_48_packaged_readership.py",
                }
            ),
        )
        self.assertIn("comfyui_h3_context/core/contract_inventory.py", scanned)
        self.assertIn("tests/test_reachability.py", scanned)
        # The M18-02 generator hard-codes eight identities in its declaration table, so it is a
        # reader of those eight in the ordinary sense and stays indexed.
        self.assertIn("scripts/fingerprint_domain.py", scanned)
        # The M18-05 generator hard-codes the five retired identities, so it stays indexed too.
        self.assertIn("scripts/retirement_eligibility.py", scanned)
        # The M23-48 generator globs the contracts directory and hard-codes no identity at all, so
        # it names nothing and stays indexed: only its *output* had to be excluded.
        self.assertIn("scripts/packaged_artifact_readership.py", scanned)


class DeterminismTests(unittest.TestCase):
    """One order, one fingerprint, and a diff a reviewer can actually read."""

    def test_entries_are_sorted_by_kind_and_identity(self) -> None:
        first = _entry(contract_id="h3.b.v1")
        second = _entry(contract_id="h3.a.v1")
        third = _entry(contract_id="h3.c.v1", kind=ContractKind.JSON_DATA)
        inventory = build_contract_inventory((first, second, third))
        self.assertEqual(
            [item.contract_id for item in inventory.entries], ["h3.c.v1", "h3.a.v1", "h3.b.v1"]
        )

    def test_a_duplicate_identity_and_kind_cannot_appear_twice(self) -> None:
        with self.assertRaises(ContractInventoryError):
            build_contract_inventory((_entry(), _entry()))

    def test_the_fingerprint_is_stable_and_content_sensitive(self) -> None:
        inventory = build_contract_inventory((_entry(), _entry(contract_id="h3.other.v1")))
        self.assertEqual(
            inventory.fingerprint, build_contract_inventory(inventory.entries).fingerprint
        )
        self.assertRegex(inventory.fingerprint, r"\Asha256:[0-9a-f]{64}\Z")
        changed = build_contract_inventory(
            (
                replace(inventory.entries[0], evidence="a different reason, same shape"),
                inventory.entries[1],
            )
        )
        self.assertNotEqual(inventory.fingerprint, changed.fingerprint)

    def test_the_entry_limit_is_enforced(self) -> None:
        self.assertEqual(MAX_INVENTORY_ENTRIES, 1_024)
        with self.assertRaises(ContractInventoryError):
            build_contract_inventory(())

    def test_regeneration_is_byte_identical(self) -> None:
        first = GENERATOR.build_inventory()
        second = GENERATOR.build_inventory()
        self.assertEqual(
            GENERATOR.artifact_bytes(first),
            GENERATOR.artifact_bytes(second),
        )
        self.assertEqual(first.fingerprint, second.fingerprint)


class CommittedArtifactTests(unittest.TestCase):
    """The shipped artifact is the generation, not a hand-edited copy of one."""

    def setUp(self) -> None:
        self.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    def test_the_committed_artifact_matches_a_fresh_generation(self) -> None:
        fresh = GENERATOR.artifact_bytes(GENERATOR.build_inventory())
        self.assertEqual(
            ARTIFACT.read_bytes(),
            fresh,
            "regenerate with `python scripts/contract_inventory.py --write`",
        )

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_artifact_declares_its_schema_and_fingerprint(self) -> None:
        self.assertEqual(self.document["schema"], CONTRACT_INVENTORY_SCHEMA)
        self.assertRegex(self.document["fingerprint"], r"\Asha256:[0-9a-f]{64}\Z")

    def test_no_entry_claims_more_than_the_rules_allow(self) -> None:
        for entry in self.document["entries"]:
            with self.subTest(contract_id=entry["contract_id"]):
                if entry["blockers"]:
                    self.assertIn(
                        entry["disposition"], {"NEED_EVIDENCE", "BLOCKED_CORRECTION_REQUIRED"}
                    )
                if not entry["consumers"]:
                    self.assertIn(
                        entry["disposition"], {"NEED_EVIDENCE", "BLOCKED_CORRECTION_REQUIRED"}
                    )
                if entry["boundary"] in {item.value for item in PROTECTED_BOUNDARIES}:
                    self.assertNotEqual(entry["disposition"], "RETIRE_CANDIDATE")
                if entry.get("reachability_source") == "injected_fixture":
                    self.assertNotEqual(entry["reachability"], "reachable")

    def test_the_artifact_carries_no_content_and_no_private_path(self) -> None:
        raw = ARTIFACT.read_text(encoding="utf-8")
        for forbidden in ("B:/", "B:\\", "A:/ComfyUI", "C:\\Users", "-----BEGIN", "Bearer "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)
        for entry in self.document["entries"]:
            self.assertNotIn("value", entry)
            self.assertNotIn("sample", entry)


class KnownFindingTests(unittest.TestCase):
    """The findings this item exists to record must survive in the shipped artifact."""

    def setUp(self) -> None:
        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        self.by_id = {entry["contract_id"]: entry for entry in document["entries"]}

    def test_the_duplicate_subgraph_identity_is_recorded_without_an_owner(self) -> None:
        entry = self.by_id["h3-context-subgraph-fixture/1"]
        self.assertIn("duplicate_schema_identity", entry["blockers"])
        self.assertIn(entry["disposition"], {"NEED_EVIDENCE", "BLOCKED_CORRECTION_REQUIRED"})
        self.assertGreater(len(entry["authority_paths"]), 1)

    def test_the_schema_without_an_identity_is_recorded_as_unresolved(self) -> None:
        entry = self.by_id["governance/contracts/training_authorization_v1.schema.json"]
        self.assertIn("unresolved_owner", entry["blockers"])
        self.assertIn(entry["disposition"], {"NEED_EVIDENCE", "BLOCKED_CORRECTION_REQUIRED"})

    def test_the_vlm_route_keeps_the_unreachability_m21_02_established(self) -> None:
        entry = self.by_id["h3.vlm.observation.v1"]
        self.assertEqual(entry["boundary"], "untrusted_input")
        self.assertEqual(entry["reachability"], "unsupported")
        self.assertEqual(entry["reachability_source"], "none")
        self.assertNotEqual(entry["disposition"], "RETIRE_CANDIDATE")

    def test_no_entry_currently_requires_a_correction(self) -> None:
        """AC-M18-01-08/09 preconditions: nothing is blocked, so nothing is rebound."""

        for entry in self.by_id.values():
            with self.subTest(contract_id=entry["contract_id"]):
                self.assertNotIn("incomplete_position_binding", entry["blockers"])
                self.assertNotEqual(entry["disposition"], "BLOCKED_CORRECTION_REQUIRED")

    def test_a_host_node_is_recorded_as_a_host_contract(self) -> None:
        entry = self.by_id["MiniMaxH3ImageToVideo"]
        self.assertEqual(entry["kind"], "host_node")
        self.assertEqual(entry["boundary"], "host_core")
        self.assertIn(entry["disposition"], {"KEEP", "NEED_EVIDENCE"})

    def test_the_cross_language_codec_is_protected(self) -> None:
        entry = self.by_id["frontend/sidebarWorkspaceCodec"]
        self.assertEqual(entry["boundary"], "cross_language")
        self.assertNotEqual(entry["disposition"], "RETIRE_CANDIDATE")

    def test_every_persisted_store_wire_is_protected(self) -> None:
        persisted = [
            entry
            for entry in self.by_id.values()
            if entry["boundary"] == "persisted" and "store" in entry["contract_id"]
        ]
        self.assertTrue(persisted)
        for entry in persisted:
            with self.subTest(contract_id=entry["contract_id"]):
                self.assertNotEqual(entry["disposition"], "RETIRE_CANDIDATE")


#: Suffixes whose files exist to *describe* the repository rather than to run in it. The scan must
#: never index any of them, so the set is named here rather than left as a single literal: adding
#: `.rst` or `.txt` to the generator later would otherwise reintroduce the defect under a new name.
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})


class ProseIsNotAConsumerTests(unittest.TestCase):
    """A document that names a contract path is describing it, not reading it.

    M23-46 is the case that proves it. The scan indexed `.md`, so a sentence added to
    `tests/TEST_SOP.md` naming `frontend/src/contracts/compositionCodec.ts` made that SOP a recorded
    consumer of the compositionCodec contract and the shipped artifact went stale on a pure prose
    edit -- discovered inside a Full Gate, in an item that had nothing to do with the inventory.
    AGENTS.md section 5.1 forbids the condition outright: a prose document must stay rewritable
    without a test turning red, and `contract_inventory.py --check` inside the gate is such a test.

    CRITICAL: the repair was to stop scanning prose, and it was proved by a manual mutation that
    left nothing behind. These two tests are that proof made permanent. Without them a
    well-intentioned re-widening of `SCANNED_SUFFIXES` -- to pick up a new documentation format, say
    -- silently re-arms the trap, and the CRITICAL comment beside that set is the only thing
    standing in the way. A comment does not fail a gate.

    Neither test asserts anything about what any document *says*, which is the same rule seen from
    the other side: prose stays free, and the artifact stays free of prose.
    """

    def test_the_scan_indexes_no_prose_suffix(self) -> None:
        indexed = {suffix.casefold() for suffix in GENERATOR.SCANNED_SUFFIXES}
        self.assertEqual(indexed & PROSE_SUFFIXES, set())

    def test_the_shipped_artifact_records_no_prose_reference(self) -> None:
        # The behavioural half. It would still hold if the suffix set were reorganized, and it
        # catches the other route to the same defect: a scanned suffix that turns out to be prose.
        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        for entry in document["entries"]:
            for field in ("authority_paths", "consumers", "producers"):
                for path in entry.get(field, ()):
                    with self.subTest(contract_id=entry["contract_id"], path=path):
                        self.assertNotIn(Path(path).suffix.casefold(), PROSE_SUFFIXES)


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    unittest.main()
