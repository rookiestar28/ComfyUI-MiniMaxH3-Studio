"""M18-05 schema retirement eligibility and removal tests.

Deleting a shipped file is the one thing in this chain that cannot be undone by regenerating an
artifact, so the tests that matter here are the ones that would have stopped the deletion.

Three carry the weight. A schema with any reader -- including a reader living in a file the contract
inventory deliberately does not scan -- must never be reported eligible, because a blind spot
arriving as a green light is exactly how a used contract gets deleted. Nothing anywhere may still
reference a retired `$id`, filename or stem. And every retired schema must name a commit that still
has it, so a stable identity stopped shipping without stopping being recoverable.

The rest fix the reviewable properties: one order, one fingerprint, bytes that match a fresh
generation, and a record that carries paths and identities and nothing else.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from comfyui_h3_context.core.contract_inventory import (
    CompatibilityDisposition as Compat,
)
from comfyui_h3_context.core.contract_inventory import (
    ContractBoundary,
    ContractDisposition,
    ContractEntry,
    ContractKind,
    PersistenceClass,
    PublicationStatus,
)
from scripts.governance.retirement_eligibility import (
    RETIREMENT_ELIGIBILITY_SCHEMA,
    RetiredSchema,
    RetirementClaim,
    RetirementEligibility,
    RetirementEligibilityError,
    RetirementVerdict,
    SchemaVerdict,
    assess_schema,
    described_wires,
    retire_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "retirement_eligibility_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "retirement_eligibility_v1.schema.json"


def _load_generator() -> Any:
    """Import the generator by path; `scripts/` is a directory, not an importable package."""

    name = "m18_05_retirement_eligibility_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "retirement_eligibility.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()


def _disposition(base: dict[str, object]) -> ContractDisposition:
    """M18-01 refuses a row that claims a disposition with no consumer behind it.

    Retirement eligibility is a question *about* consumer-free rows, so nearly every fixture
    here has an empty consumer tuple and must fail closed the way the real inventory does.
    """

    return ContractDisposition.KEEP if base["consumers"] else ContractDisposition.NEED_EVIDENCE


def _entry(**overrides: object) -> ContractEntry:
    """A minimal well-formed inventory row; each test names only the facet it is about."""

    base: dict[str, object] = {
        "contract_id": "comfyui-h3-context://contracts/example_v1.schema.json",
        "kind": ContractKind.JSON_SCHEMA,
        "authority_paths": ("comfyui_h3_context/contracts/example_v1.schema.json",),
        "dialect": "https://json-schema.org/draft/2020-12/schema",
        "producers": ("comfyui_h3_context/contracts/example_v1.schema.json",),
        "consumers": (),
        "boundary": ContractBoundary.PUBLIC,
        "persistence": PersistenceClass.TRANSIENT,
        "publication": PublicationStatus.PACKAGED,
        "reachability": None,
        "reachability_source": None,
        "fingerprint_domain": "wire_contract",
        "disposition": ContractDisposition.KEEP,
        "compatibility": Compat.OLD_READERS_UNAFFECTED,
        "evidence": "an example row",
    }
    base.update(overrides)
    if "disposition" not in overrides:
        base["disposition"] = _disposition(base)
    return ContractEntry(**base)  # type: ignore[arg-type]


def _wire(**overrides: object) -> ContractEntry:
    base: dict[str, object] = {
        "contract_id": "h3.example.v1",
        "kind": ContractKind.PYTHON_WIRE,
        "authority_paths": ("comfyui_h3_context/core/example.py",),
        "dialect": "not_applicable",
        "producers": ("comfyui_h3_context/core/example.py",),
        "consumers": (),
        "boundary": ContractBoundary.PROCESS_LOCAL,
        "persistence": PersistenceClass.TRANSIENT,
        "publication": PublicationStatus.PACKAGED,
        "reachability": None,
        "reachability_source": None,
        "fingerprint_domain": "semantic",
        "disposition": ContractDisposition.KEEP,
        "compatibility": Compat.OLD_READERS_UNAFFECTED,
        "evidence": "an example wire",
    }
    base.update(overrides)
    if "disposition" not in overrides:
        base["disposition"] = _disposition(base)
    return ContractEntry(**base)  # type: ignore[arg-type]


class EligibilityRuleTests(unittest.TestCase):
    """What may be called eligible, and -- much more importantly -- what may not."""

    def test_a_redundant_process_local_wire_with_no_reader_is_eligible(self) -> None:
        verdict = assess_schema(_entry(), (_wire(),))
        self.assertIs(verdict.verdict, RetirementVerdict.ELIGIBLE)
        self.assertEqual(verdict.reasons, ())
        self.assertEqual(verdict.described_wires, ("h3.example.v1",))

    def test_a_wire_behind_an_aggregation_surface_is_still_paired_with_its_schema(self) -> None:
        """M19-06: the stem convention has to be read through a re-exporting surface.

        `described_wires` pairs `X_v1.schema.json` with wires whose authority module is `X.py`.
        M19-05 split three large modules into layers behind an aggregation surface, so a wire
        constant that lived in `X.py` moved to `X_primitives.py` while the schema is still named
        after `X`. Without the map the pairing silently empties and the schema reports having no
        redundancy to remove -- conservative, but false.
        """

        schema = _entry(authority_paths=("comfyui_h3_context/contracts/example_v1.schema.json",))
        moved = _wire(authority_paths=("comfyui_h3_context/core/example_primitives.py",))

        self.assertEqual(described_wires(schema, (moved,)), ())
        self.assertEqual(
            described_wires(schema, (moved,), reexported_by={"example_primitives": ("example",)}),
            ("h3.example.v1",),
        )

    def test_the_surface_map_pairs_nothing_it_was_not_given(self) -> None:
        """The rejected fix matched consumers and invented pairings; this one cannot."""

        schema = _entry(authority_paths=("comfyui_h3_context/contracts/example_v1.schema.json",))
        unrelated = _wire(authority_paths=("comfyui_h3_context/core/unrelated.py",))
        self.assertEqual(
            described_wires(schema, (unrelated,), reexported_by={"other": ("example",)}),
            (),
        )

    def test_a_hidden_reader_is_counted_exactly_like_a_visible_one(self) -> None:
        """The blind spot that reported a used schema as safe to delete, now closed.

        M18-01 keeps a few files out of its scan so the inventory cannot supply its own evidence.
        A schema those files read shows zero consumers, and zero consumers is what this record calls
        eligible. The first generated draft said `contract_inventory_v1.schema.json` was eligible
        while `tests/test_contract_inventory.py` was reading it.
        """

        verdict = assess_schema(
            _entry(), (_wire(),), hidden_consumers=("tests/test_contract_inventory.py",)
        )
        self.assertIs(verdict.verdict, RetirementVerdict.ELIGIBLE_WITH_CONSUMERS)
        self.assertEqual(verdict.hidden_consumers, ("tests/test_contract_inventory.py",))
        self.assertIn("tests/test_contract_inventory.py", verdict.internal_consumers)
        self.assertTrue(verdict.reasons)

    def test_a_reader_outside_tests_and_scripts_blocks_outright(self) -> None:
        verdict = assess_schema(
            _entry(consumers=("comfyui_h3_context/core/reader.py",)), (_wire(),)
        )
        self.assertIs(verdict.verdict, RetirementVerdict.BLOCKED)
        self.assertEqual(verdict.external_consumers, ("comfyui_h3_context/core/reader.py",))

    def test_a_protected_boundary_on_the_described_wire_blocks(self) -> None:
        for boundary in (
            ContractBoundary.PERSISTED,
            ContractBoundary.UNTRUSTED_INPUT,
            ContractBoundary.CROSS_LANGUAGE,
            ContractBoundary.PUBLIC,
            ContractBoundary.HOST_CORE,
        ):
            with self.subTest(boundary=boundary.value):
                verdict = assess_schema(_entry(), (_wire(boundary=boundary),))
                self.assertIs(verdict.verdict, RetirementVerdict.BLOCKED)
                self.assertTrue(any(boundary.value in reason for reason in verdict.reasons))

    def test_a_persisted_schema_blocks_even_with_a_process_local_wire(self) -> None:
        verdict = assess_schema(_entry(persistence=PersistenceClass.PERSISTED), (_wire(),))
        self.assertIs(verdict.verdict, RetirementVerdict.BLOCKED)

    def test_a_schema_describing_no_wire_reports_no_redundancy(self) -> None:
        verdict = assess_schema(_entry(), ())
        self.assertIs(verdict.verdict, RetirementVerdict.NO_REDUNDANCY)
        self.assertTrue(verdict.reasons)

    def test_only_an_eligible_verdict_may_be_silent_about_why(self) -> None:
        with self.assertRaises(RetirementEligibilityError):
            SchemaVerdict(
                contract_id="h3.example.v1",
                schema_path="comfyui_h3_context/contracts/example_v1.schema.json",
                verdict=RetirementVerdict.ELIGIBLE,
                described_wires=(),
                internal_consumers=(),
                external_consumers=(),
                reasons=("a reason an eligible verdict may not carry",),
            )
        with self.assertRaises(RetirementEligibilityError):
            SchemaVerdict(
                contract_id="h3.example.v1",
                schema_path="comfyui_h3_context/contracts/example_v1.schema.json",
                verdict=RetirementVerdict.BLOCKED,
                described_wires=(),
                internal_consumers=(),
                external_consumers=(),
            )

    def test_the_wire_pairing_follows_the_module_naming_convention(self) -> None:
        self.assertEqual(described_wires(_entry(), (_wire(),)), ("h3.example.v1",))
        other = _wire(authority_paths=("comfyui_h3_context/core/unrelated.py",))
        self.assertEqual(described_wires(_entry(), (other,)), ())

    def test_assess_schema_refuses_a_non_schema_entry(self) -> None:
        with self.assertRaises(RetirementEligibilityError):
            assess_schema(_wire(), ())


def _claim(**overrides: object) -> RetirementClaim:
    base: dict[str, object] = {
        "contract_id": "comfyui-h3-context://contracts/example_v1.schema.json",
        "schema_path": "comfyui_h3_context/contracts/example_v1.schema.json",
        "rollback_commit": "0" * 40,
        "surviving_authority": "comfyui_h3_context/core/example.py",
        "item": "M18-05",
    }
    base.update(overrides)
    return RetirementClaim(**base)  # type: ignore[arg-type]


def _retired(**overrides: object) -> RetiredSchema:
    """A well-formed retirement, as `retire_schema` would compute it for the fixture wire."""

    return retire_schema(
        _claim(**{key: value for key, value in overrides.items() if key in _CLAIM_FIELDS}),
        (_wire(),),
        surviving_references=overrides.get("surviving_references", ()),  # type: ignore[arg-type]
    )


_CLAIM_FIELDS = frozenset(
    {"contract_id", "schema_path", "rollback_commit", "surviving_authority", "item"}
)


class RetirementDerivationTests(unittest.TestCase):
    """A retirement is a checked claim. The checks are the point, so they are tested directly."""

    def test_the_evidence_is_recomputed_rather_than_declared(self) -> None:
        retired = retire_schema(_claim(), (_wire(),))
        self.assertEqual(retired.described_wires, ("h3.example.v1",))
        self.assertEqual(retired.wire_boundaries, ("process_local",))
        self.assertEqual(retired.wire_persistence, ("transient",))
        self.assertEqual(retired.surviving_references, ())

    def test_a_claim_whose_authority_owns_no_wire_is_refused(self) -> None:
        """The case that caught a real mistake in this item's own removal set.

        `official_context_ir_v1.schema.json` was in the hand-written removal set, but
        `core/official_context_ir.py` declares no versioned wire, so the inventory never listed one
        and the schema's verdict was `no_redundancy` -- never `eligible`. The moment the rule became
        executable it refused the claim, and the file was restored.
        """

        with self.assertRaises(RetirementEligibilityError):
            retire_schema(_claim(surviving_authority="comfyui_h3_context/core/nothing.py"), ())

    def test_a_claim_whose_wire_is_not_process_local_and_transient_is_refused(self) -> None:
        for override in (
            {"boundary": ContractBoundary.PERSISTED},
            {"boundary": ContractBoundary.UNTRUSTED_INPUT},
            {"persistence": PersistenceClass.PERSISTED},
        ):
            with self.subTest(**{k: getattr(v, "value", v) for k, v in override.items()}):
                with self.assertRaises(RetirementEligibilityError):
                    retire_schema(_claim(), (_wire(**override),))

    def test_a_claim_something_still_references_is_refused(self) -> None:
        """The zero-consumer rule, re-derived rather than remembered.

        Whether anything still names an identity is a property of the rest of the repository, so it
        survives the deletion intact and can be measured every time the record is built.
        """

        with self.assertRaises(RetirementEligibilityError):
            retire_schema(
                _claim(),
                (_wire(),),
                surviving_references=("docs/H3_EXAMPLE.md",),
            )

    def test_retire_schema_refuses_anything_that_is_not_a_claim(self) -> None:
        with self.assertRaises(RetirementEligibilityError):
            retire_schema(_wire(), (_wire(),))  # type: ignore[arg-type]


class RecordGuardTests(unittest.TestCase):
    """A record that accepts a malformed claim cannot be evidence for deleting anything."""

    def test_a_retired_schema_may_not_also_carry_a_live_verdict(self) -> None:
        verdict = assess_schema(_entry(), (_wire(),))
        retired = _retired(
            contract_id=verdict.contract_id,
            schema_path=verdict.schema_path,
        )
        with self.assertRaises(RetirementEligibilityError):
            RetirementEligibility(verdicts=(verdict,), retired=(retired,))

    def test_a_retirement_without_a_usable_rollback_target_is_refused(self) -> None:
        for commit in ("", "not-a-commit", "abc123", "0" * 39, "0" * 41):
            with self.subTest(commit=commit), self.assertRaises(RetirementEligibilityError):
                _claim(rollback_commit=commit)

    def test_a_traversing_or_absolute_path_is_refused(self) -> None:
        for path in ("../secrets/example_v1.schema.json", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(RetirementEligibilityError):
                _claim(schema_path=path)

    def test_verdicts_must_be_sorted_and_unique(self) -> None:
        first = assess_schema(_entry(contract_id="h3.a.v1"), ())
        second = assess_schema(_entry(contract_id="h3.b.v1"), ())
        with self.assertRaises(RetirementEligibilityError):
            RetirementEligibility(verdicts=(second, first))
        with self.assertRaises(RetirementEligibilityError):
            RetirementEligibility(verdicts=(first, first))

    def test_an_unsupported_schema_is_refused(self) -> None:
        verdict = assess_schema(_entry(), ())
        with self.assertRaises(RetirementEligibilityError):
            RetirementEligibility(verdicts=(verdict,), schema="h3-context-retirement-eligibility/2")

    def test_the_fingerprint_answers_to_the_verdicts_and_the_retirements(self) -> None:
        verdict = assess_schema(_entry(), (_wire(),))
        base = RetirementEligibility(verdicts=(verdict,))
        same = RetirementEligibility(verdicts=(verdict,))
        self.assertEqual(base.fingerprint, same.fingerprint)
        retired = _retired(
            contract_id="comfyui-h3-context://contracts/other_v1.schema.json",
            schema_path="comfyui_h3_context/contracts/other_v1.schema.json",
        )
        self.assertNotEqual(
            RetirementEligibility(verdicts=(verdict,), retired=(retired,)).fingerprint,
            base.fingerprint,
        )


class GeneratedRecordTests(unittest.TestCase):
    """The committed record, and the repository it claims to describe."""

    document: dict[str, Any]
    record: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.record = GENERATOR.build_eligibility()

    def test_the_artifact_regenerates_byte_identically(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.record), ARTIFACT.read_bytes().replace(b"\r\n", b"\n")
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], RETIREMENT_ELIGIBILITY_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.record.fingerprint)
        ids = [item["contract_id"] for item in self.document["verdicts"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_nothing_is_reported_eligible_while_a_reader_exists(self) -> None:
        """The safety property. An eligible verdict is a licence to delete a shipped file."""

        for item in self.document["verdicts"]:
            with self.subTest(contract=item["contract_id"]):
                if item["verdict"] == "eligible":
                    self.assertEqual(item["internal_consumers"], [])
                    self.assertEqual(item["external_consumers"], [])
                    self.assertEqual(item["hidden_consumers"], [])

    def test_every_non_eligible_verdict_says_what_blocked_it(self) -> None:
        for item in self.document["verdicts"]:
            with self.subTest(contract=item["contract_id"]):
                if item["verdict"] == "eligible":
                    self.assertEqual(item["reasons"], [])
                else:
                    self.assertTrue(item["reasons"])

    def test_the_record_carries_identities_and_paths_and_nothing_else(self) -> None:
        """Field by field, the way M18-01 checks its own record.

        A whole-file scan is the wrong instrument here: one shipped `$id` is spelled
        `https://comfyui-h3-context.invalid/...`, which is a repository-owned identity admitted by
        namespace, not a locator. Everything that is not such an identity is held to the full rule.
        """

        from comfyui_h3_context.core.contract_inventory import (
            FORBIDDEN_RECORD_TEXT,
            KNOWN_IDENTITY_NAMESPACES,
        )

        def _check(value: str, *, is_identity: bool) -> None:
            if is_identity and value.startswith(KNOWN_IDENTITY_NAMESPACES):
                return
            match = FORBIDDEN_RECORD_TEXT.search(value)
            self.assertIsNone(match, f"{value!r} -> {match.group(0) if match else ''}")

        identity_fields = {"contract_id"}
        list_fields = (
            "described_wires",
            "internal_consumers",
            "external_consumers",
            "hidden_consumers",
            "reasons",
        )
        for item in self.document["verdicts"]:
            with self.subTest(contract=item["contract_id"]):
                for field in ("contract_id", "schema_path", "verdict"):
                    _check(item[field], is_identity=field in identity_fields)
                for field in list_fields:
                    for value in item[field]:
                        _check(value, is_identity=field == "described_wires")
        retired_identity_fields = {"contract_id", "described_wires"}
        for item in self.document["retired"]:
            with self.subTest(retired=item["contract_id"]):
                for field, value in item.items():
                    is_identity = field in retired_identity_fields
                    for text in value if isinstance(value, list) else [value]:
                        _check(text, is_identity=is_identity)
        for path_field in ("schema_path", "surviving_authority"):
            for item in self.document["retired"]:
                self.assertFalse(item[path_field].startswith("/"))
                self.assertNotIn("..", item[path_field].split("/"))


class RetirementTests(unittest.TestCase):
    """AC-M18-05-03/04/05: nothing lost, nothing dangling, nothing silent."""

    document: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    def test_the_retired_schemas_are_recorded_and_gone(self) -> None:
        retired = self.document["retired"]
        self.assertEqual(len(retired), 4)
        for item in retired:
            with self.subTest(contract=item["contract_id"]):
                self.assertFalse((REPO_ROOT / item["schema_path"]).exists())
                self.assertTrue((REPO_ROOT / item["surviving_authority"]).is_file())
                self.assertEqual(item["item"], "M18-05")
                self.assertTrue(item["described_wires"])
                self.assertEqual(item["wire_boundaries"], ["process_local"])
                self.assertEqual(item["wire_persistence"], ["transient"])
                self.assertEqual(item["surviving_references"], [])

    def test_a_schema_the_rule_does_not_reach_was_not_retired(self) -> None:
        """A schema in the hand-written removal set that the executable rule never reached.

        `core/official_context_ir.py` declares no versioned wire, so the inventory lists none and
        the schema's verdict is `no_redundancy` -- it was never in the eligible set the plan's rule
        derives. It describes the outbound provider request projection, which is the last boundary
        to delete a public schema from on unexamined evidence, so the file stays and the record says
        why in the ordinary way: as a verdict.
        """

        retired_ids = {item["contract_id"] for item in self.document["retired"]}
        contract_id = "comfyui-h3-context://contracts/official_context_ir_v1.schema.json"
        self.assertNotIn(contract_id, retired_ids)
        verdict = next(
            item for item in self.document["verdicts"] if item["contract_id"] == contract_id
        )
        self.assertEqual(verdict["verdict"], "no_redundancy")
        self.assertEqual(verdict["described_wires"], [])
        restored = REPO_ROOT / "governance/contracts/official_context_ir_v1.schema.json"
        self.assertTrue(restored.is_file())

    def test_every_retired_schema_is_recoverable_from_its_rollback_commit(self) -> None:
        """A stable `$id` may stop shipping; it may not stop being recoverable."""

        for item in self.document["retired"]:
            with self.subTest(contract=item["contract_id"]):
                result = subprocess.run(
                    ["git", "cat-file", "-e", f"{item['rollback_commit']}:{item['schema_path']}"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "ignore"))

    def test_no_dangling_reference_to_a_retired_schema_survives(self) -> None:
        """AC-M18-05-05. The three discovery rules the inventory uses, applied in reverse.

        `examples/` is in the sweep even though it holds no contract: `MANIFEST.in` ships it
        and the contract inventory's own scan roots stop short of it, so a dangling reference
        written into a shipped example is exactly the kind this test has to be the one to
        catch. Prose documents are excluded -- they carry no test contract in this repository.
        """

        roots = (
            REPO_ROOT / "comfyui_h3_context",
            REPO_ROOT / "examples",
            REPO_ROOT / "frontend" / "src",
            REPO_ROOT / "frontend" / "tests",
            REPO_ROOT / "tests",
            REPO_ROOT / "scripts",
            REPO_ROOT / "workflows",
            REPO_ROOT / "subgraphs",
        )
        # The record and its generator name the retired identities on purpose -- that naming *is*
        # the deprecation notice AC-M18-05-04 requires. This test file is deliberately not exempt:
        # it reads the identities from the record rather than repeating them, so if a literal ever
        # appears here the assertion below should fail like anywhere else.
        allowed = {
            (REPO_ROOT / "governance" / "contracts" / "retirement_eligibility_v1.json"),
            (REPO_ROOT / "scripts" / "retirement_eligibility.py"),
        }
        needles: set[str] = set()
        for item in self.document["retired"]:
            name = item["schema_path"].rsplit("/", 1)[-1]
            needles.update({item["contract_id"], name, name.removesuffix(".schema.json")})
        offenders: list[str] = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or path in allowed:
                    continue
                if path.suffix not in {".py", ".ts", ".tsx", ".json", ".toml"}:
                    continue
                if "node_modules" in path.parts or "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for needle in needles:
                    if needle in text:
                        offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {needle}")
        self.assertEqual(offenders, [])

    def test_each_retired_schemas_shape_is_still_enforced_by_its_typed_authority(self) -> None:
        """AC-M18-05-02. The schema enforced nothing; the typed module always did, and still does.

        One concrete refusal per retired authority, chosen to be the kind of malformed value the
        deleted schema was nominally there to catch: a wrong enum, an unbounded identifier, a value
        that is not a report, an unsupported schema tag.
        """

        from comfyui_h3_context.core.base_assistant import BaseAssistantControls

        # The authorities re-raise from the shared hierarchy in `core/errors.py`; importing the
        # exception types from there rather than from each module keeps this test off names those
        # modules do not export.
        from comfyui_h3_context.core.errors import (
            BaseAssistantMigrationError,
            ExecutionCoordinatorError,
            ReportLifecycleError,
        )
        from comfyui_h3_context.core.execution_coordinator import CoordinatorCapacity
        from comfyui_h3_context.core.ui_projection import ExecutionCorrelation
        from comfyui_h3_context.core.validation_lifecycle import ValidatedReportEnvelope

        with self.subTest(authority="base_assistant"):
            with self.assertRaises(BaseAssistantMigrationError):
                BaseAssistantControls(
                    task_mode="t2va",  # type: ignore[arg-type]
                    user_intent="a bounded intent",
                    duration_milliseconds=6_000,
                )

        with self.subTest(authority="ui_projection"):
            with self.assertRaises(ReportLifecycleError):
                ExecutionCorrelation(prompt_id="a" * 4096, execution_node_id="1")

        with self.subTest(authority="validation_lifecycle"):
            with self.assertRaises(ReportLifecycleError):
                ValidatedReportEnvelope.from_report(object())  # type: ignore[arg-type]

        with self.subTest(authority="execution_coordinator"):
            with self.assertRaises(ExecutionCoordinatorError):
                CoordinatorCapacity(
                    cpu_threads=1,
                    gpu_slots=0,
                    ram_bytes=1,
                    vram_bytes=0,
                    max_concurrency=1,
                    schema="h3-context-execution-coordinator/999",
                )

    def test_the_one_invariant_the_typed_authority_was_missing_is_restored(self) -> None:
        """AC-M18-05-02 was not free: one retired schema stated something the module did not.

        The retired UI-projection schema required `severity` to be one of four values. The
        typed authority only bounded its length, so any 32-character string crossed the UI
        redaction boundary calling itself a severity. `core/ui_projection.py` now checks
        membership against the live `ValidationSeverity`, so the invariant survives the schema
        rather than dying with it -- and against the enum, not a copied list, so the two
        cannot drift again.

        The behavioural test lives with the fixture in `tests/test_ui_projection_lifecycle.py`;
        what belongs here is the parity claim that made the gap findable in the first place.
        """

        from comfyui_h3_context.core.contracts import ValidationSeverity

        # The enum the deleted schema stated, transcribed from `42a8e3b`.
        self.assertEqual(
            {member.value for member in ValidationSeverity},
            {"info", "warning", "error", "fatal"},
        )


if __name__ == "__main__":  # pragma: no cover - convenience for a single-file run
    unittest.main()
