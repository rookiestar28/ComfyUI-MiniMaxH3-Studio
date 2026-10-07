from __future__ import annotations

import copy
import importlib
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
# CRITICAL: a document and its schema no longer share a directory. The census and the shape fixture
# are compiled into the frontend bundle, so M23-55 kept them in the installed package; nothing in
# the shipped product opens their schemas, so those moved to `governance/`, which the wheel, the
# sdist and the Registry archive exclude. Spell both roots out. These four were `with_name` siblings
# of each other until the relocation, and a derived sibling is invisible to any per-path rewrite --
# the path never appears in the source, so the move left it pointing at a file that no longer exists
# and only the Full Gate found it.
CENSUS_PATH = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_census_v1.json"
FIXTURE_PATH = ROOT / "comfyui_h3_context" / "contracts" / "host_seam_shape_fixture_v1.json"
CENSUS_SCHEMA_PATH = ROOT / "governance" / "contracts" / "host_seam_census_v1.schema.json"
FIXTURE_SCHEMA_PATH = ROOT / "governance" / "contracts" / "host_seam_shape_fixture_v1.schema.json"


VALID_CENSUS: dict[str, Any] = {
    "schema": "h3.context.host_seam_census.v1",
    "profile": "comfyui_host_seams_v1",
    "seams": [
        {
            "id": "frontend.app.graph",
            "layer": "frontend",
            "owner": "frontend/src/host/appMode.ts",
            "classification": "documented",
            "authority": "official.comfy_objects",
            "source_paths": ["frontend/src/entry.tsx", "frontend/src/host/appMode.ts"],
            "readiness_states": ["ready", "unavailable"],
            "cost_class": "constant",
            "bound": None,
        },
        {
            "id": "frontend.litegraph.registered_node_types",
            "layer": "frontend",
            "owner": "frontend/src/host/hostSeamContract.ts",
            "classification": "undocumented_but_observed",
            "authority": "observed.hc_09_pinned_host",
            "source_paths": [
                "frontend/src/host/appMode.ts",
                "frontend/src/host/hostSeamContract.ts",
                "frontend/src/host/officialAssetResolution.ts",
            ],
            "readiness_states": [
                "absent",
                "present_not_ready",
                "ready",
                "unavailable",
            ],
            "cost_class": "full_collection",
            "bound": {
                "observed_floor": 6381,
                "multiplier": 2,
                "derived_ceiling": 12762,
                "absolute_ceiling": 50000,
            },
        },
    ],
}

VALID_FIXTURE: dict[str, Any] = {
    "schema": "h3.context.host_seam_shape_fixture.v1",
    "profile": "comfyui_host_seams_v1",
    "subject": {
        "comfyui_version": "0.32.0",
        "comfyui_revision": "b323a345bbbfb2f3a95b5b73b68eb7919a26515e",  # pragma: allowlist secret
        "frontend_version": "1.48.7",
        "fixture_version": 1,
    },
    "observations": [
        {
            "seam_id": "frontend.app.graph",
            "presence": "present",
            "kind": "object",
            "key_shape": "closed_members",
            "element_kind": "none",
            "readiness_state": "ready",
            "count_bucket": "one",
            "byte_bucket": "not_measured",
            "latency_bucket": "sub_10ms",
        },
        {
            "seam_id": "frontend.litegraph.registered_node_types",
            "presence": "present",
            "kind": "mapping",
            "key_shape": "node_type",
            "element_kind": "node_definition_wrapper",
            "readiness_state": "ready",
            "count_bucket": "thousands",
            "byte_bucket": "not_measured",
            "latency_bucket": "sub_10ms",
        },
    ],
}


def subject_module() -> ModuleType:
    try:
        return importlib.import_module("comfyui_h3_context.core.host_seam_contract")
    except ModuleNotFoundError as error:
        raise AssertionError("HC-09 host seam contract implementation is missing") from error


class HostSeamContractTests(unittest.TestCase):
    def test_valid_census_and_content_free_fixture_join_exactly(self) -> None:
        module = subject_module()
        contract = module.parse_host_seam_contract(VALID_CENSUS, VALID_FIXTURE)
        self.assertEqual(contract.profile, "comfyui_host_seams_v1")
        self.assertEqual(
            tuple(row.seam_id for row in contract.rows),
            (
                "frontend.app.graph",
                "frontend.litegraph.registered_node_types",
            ),
        )
        self.assertEqual(contract.rows[1].bound.derived_ceiling, 12762)

    def test_unknown_duplicate_and_unjoined_rows_fail_closed(self) -> None:
        module = subject_module()
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []

        unknown = copy.deepcopy(VALID_FIXTURE)
        unknown["observations"][0]["arbitrary_host_value"] = "not allowed"
        candidates.append((VALID_CENSUS, unknown))

        duplicate = copy.deepcopy(VALID_CENSUS)
        duplicate["seams"].append(copy.deepcopy(duplicate["seams"][0]))
        candidates.append((duplicate, VALID_FIXTURE))

        unjoined = copy.deepcopy(VALID_FIXTURE)
        unjoined["observations"].pop()
        candidates.append((VALID_CENSUS, unjoined))

        for census, fixture in candidates:
            with self.subTest(candidate=len(candidates)):
                with self.assertRaises(module.HostSeamContractError):
                    module.parse_host_seam_contract(census, fixture)

    def test_classification_authority_paths_and_subject_are_bounded(self) -> None:
        module = subject_module()
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []

        wrong_authority = copy.deepcopy(VALID_CENSUS)
        wrong_authority["seams"][0]["authority"] = "observed.hc_09_pinned_host"
        candidates.append((wrong_authority, VALID_FIXTURE))

        escaping_path = copy.deepcopy(VALID_CENSUS)
        escaping_path["seams"][0]["source_paths"] = ["../outside.ts"]
        candidates.append((escaping_path, VALID_FIXTURE))

        private_subject = copy.deepcopy(VALID_FIXTURE)
        private_subject["subject"]["frontend_version"] = "https://private.invalid/value"
        candidates.append((VALID_CENSUS, private_subject))

        for census, fixture in candidates:
            with self.assertRaises(module.HostSeamContractError):
                module.parse_host_seam_contract(census, fixture)

    def test_readiness_states_and_measured_bound_formula_are_not_implicit(self) -> None:
        module = subject_module()
        invalid_states = copy.deepcopy(VALID_CENSUS)
        invalid_states["seams"][1]["readiness_states"] = ["absent", "ready"]
        with self.assertRaises(module.HostSeamContractError):
            module.parse_host_seam_contract(invalid_states, VALID_FIXTURE)

        invalid_bound = copy.deepcopy(VALID_CENSUS)
        invalid_bound["seams"][1]["bound"]["derived_ceiling"] = 10000
        with self.assertRaises(module.HostSeamContractError):
            module.parse_host_seam_contract(invalid_bound, VALID_FIXTURE)

        above_absolute = copy.deepcopy(VALID_CENSUS)
        above_absolute["seams"][1]["bound"]["absolute_ceiling"] = 12000
        with self.assertRaises(module.HostSeamContractError):
            module.parse_host_seam_contract(above_absolute, VALID_FIXTURE)

        multiplier_overflow = copy.deepcopy(VALID_CENSUS)
        multiplier_overflow["seams"][1]["bound"]["multiplier"] = 17
        multiplier_overflow["seams"][1]["bound"]["derived_ceiling"] = 50000
        with self.assertRaises(module.HostSeamContractError):
            module.parse_host_seam_contract(multiplier_overflow, VALID_FIXTURE)

        hostile_floor = copy.deepcopy(VALID_CENSUS)
        hostile_floor["seams"][1]["bound"]["observed_floor"] = 1000001
        hostile_floor["seams"][1]["bound"]["derived_ceiling"] = 50000
        with self.assertRaises(module.HostSeamContractError):
            module.parse_host_seam_contract(hostile_floor, VALID_FIXTURE)

    def test_mapping_readiness_distinguishes_absent_not_ready_ready_and_unavailable(self) -> None:
        module = subject_module()
        classify = module.classify_mapping_readiness
        self.assertEqual(classify(None, readiness_reached=False).value, "absent")
        self.assertEqual(classify({}, readiness_reached=False).value, "present_not_ready")
        self.assertEqual(classify({"Synthetic": object()}, readiness_reached=True).value, "ready")
        self.assertEqual(classify({}, readiness_reached=True).value, "unavailable")
        self.assertEqual(classify([], readiness_reached=True).value, "unavailable")

    def test_tracked_census_and_fixture_parse_without_private_shape_values(self) -> None:
        module = subject_module()
        self.assertTrue(CENSUS_PATH.is_file(), "tracked HC-09 census is missing")
        self.assertTrue(FIXTURE_PATH.is_file(), "tracked HC-09 shape fixture is missing")
        contract = module.parse_host_seam_contract(
            json.loads(CENSUS_PATH.read_text(encoding="utf-8")),
            json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
        )
        self.assertGreaterEqual(len(contract.rows), 12)
        fixture_text = FIXTURE_PATH.read_text(encoding="utf-8").lower()
        for forbidden in ("http://", "https://", "\\\\", ":\\", "cookie", "bearer"):
            self.assertNotIn(forbidden, fixture_text)
        raw_fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        forbidden_keys = {"prompt", "prompt_text", "workflow", "media", "token", "url", "path"}

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        self.assertFalse(keys(raw_fixture) & forbidden_keys)

    def test_tracked_documents_validate_against_closed_json_schemas(self) -> None:
        for document_path, schema_path in (
            (CENSUS_PATH, CENSUS_SCHEMA_PATH),
            (FIXTURE_PATH, FIXTURE_SCHEMA_PATH),
        ):
            with self.subTest(document=document_path.name):
                document = json.loads(document_path.read_text(encoding="utf-8"))
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                self.assertEqual(list(Draft202012Validator(schema).iter_errors(document)), [])


if __name__ == "__main__":
    unittest.main()
