"""M18-04 compact public manifest and v1 projection tests.

The claim is that v1 loses nothing by becoming a projection of v2, and the only honest way to check
that is byte equality: the projected v1 wire must equal the wire v1 builds for itself, and the v1
fingerprint must not move. Everything else here exists because a compaction can be byte-correct and
still be wrong in the ways that matter.

Three things carry the weight. The projection must be byte-identical, or a consumer somewhere reads
a manifest that changed under it. The derived views must still *refuse* what they refused before --
a missing node, an unknown node, a reordered registry -- because a view that is cheaper and blinder
is not the same view. And a fixture's node inventory must come from the fixture file, with anything
in neither the repository registry nor the declared host-core set failing closed, since an
unrecognised node type is exactly where "we did not see it" must not read as "it is fine".

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from comfyui_h3_context.core.errors import PublicManifestError
from comfyui_h3_context.core.native_h3 import (
    NATIVE_H3_IMAGE_NODE_ID,
    NATIVE_H3_REFERENCE_NODE_ID,
)
from comfyui_h3_context.core.public_manifest import (
    DEFAULT_WORKFLOW_FIXTURES,
    PUBLIC_MANIFEST_SCHEMA,
)
from comfyui_h3_context.core.public_manifest_v2 import (
    DEFAULT_FIXTURE_NODE_INVENTORY,
    HOST_CORE_NODE_TYPES,
    PUBLIC_MANIFEST_V2_SCHEMA,
    PublicManifestV2,
    RuntimeNodeFacts,
    WorkflowFixtureDescriptor,
    build_public_manifest_v2,
    derive_binding_manifest,
    extract_fixture_node_types,
    project_public_manifest_v1,
)
from comfyui_h3_context.public_api import (
    build_runtime_public_manifest_v2,
    get_public_manifest,
    get_public_manifest_v2,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "governance" / "contracts" / "public_manifest_v2.schema.json"
V1_SCHEMA_PATH = ROOT / "governance" / "contracts" / "public_manifest_v1.schema.json"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class ProjectionTests(unittest.TestCase):
    """AC-M18-04-03: v1 does not change. It is produced differently, and that is all."""

    v1: Any
    v2: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.v1 = get_public_manifest()
        cls.v2 = get_public_manifest_v2()

    def test_the_projected_v1_wire_is_byte_identical(self) -> None:
        projected = project_public_manifest_v1(self.v2)
        self.assertEqual(_canonical(projected.to_wire()), _canonical(self.v1.to_wire()))

    def test_the_v1_fingerprint_does_not_move(self) -> None:
        self.assertEqual(project_public_manifest_v1(self.v2).fingerprint, self.v1.fingerprint)

    def test_every_derived_section_matches_the_section_v1_built(self) -> None:
        projected = project_public_manifest_v1(self.v2)
        for section in (
            "contracts",
            "bindings",
            "reachability",
            "object_info",
            "workflow_fixtures",
        ):
            with self.subTest(section=section):
                self.assertEqual(
                    _canonical(projected.to_wire()[section]),
                    _canonical(self.v1.to_wire()[section]),
                )

    def test_the_projection_is_deterministic(self) -> None:
        first = project_public_manifest_v1(self.v2).to_wire()
        second = project_public_manifest_v1(build_runtime_public_manifest_v2()).to_wire()
        self.assertEqual(_canonical(first), _canonical(second))

    def test_the_compaction_is_real_and_lossless(self) -> None:
        """The reduction is the size of material that was never an authority, not a compression."""

        v1_bytes = len(_canonical(self.v1.to_wire()))
        v2_bytes = len(_canonical(self.v2.to_wire()))
        self.assertLess(v2_bytes, v1_bytes // 3)
        self.assertEqual(
            _canonical(project_public_manifest_v1(self.v2).to_wire()),
            _canonical(self.v1.to_wire()),
        )

    def test_a_v1_document_is_never_read_as_v2_or_the_reverse(self) -> None:
        self.assertNotEqual(PUBLIC_MANIFEST_SCHEMA, PUBLIC_MANIFEST_V2_SCHEMA)
        v1_schema = json.loads(V1_SCHEMA_PATH.read_text(encoding="utf-8"))
        v2_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(v1_schema["$id"], v2_schema["$id"])
        import jsonschema

        v2_validator = jsonschema.Draft202012Validator(v2_schema)
        v1_validator = jsonschema.Draft202012Validator(v1_schema)
        self.assertTrue(list(v2_validator.iter_errors(self.v1.to_wire())))
        self.assertTrue(list(v1_validator.iter_errors(self.v2.to_wire())))

    def test_the_v2_wire_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.v2.to_wire()))
        self.assertEqual([error.message for error in errors], [])

    def test_the_v2_wire_carries_no_derived_section(self) -> None:
        """AC-M18-04-01: a derived view must not be serialized as a parallel authority."""

        wire = self.v2.to_wire()
        for absent in ("bindings", "reachability", "object_info"):
            self.assertNotIn(absent, wire)
        for fixture in wire["workflow_fixtures"]:
            self.assertNotIn("node_ids", fixture)


class DerivedViewTests(unittest.TestCase):
    """AC-M18-04-02: a cheaper view that refuses less is not the same view."""

    v2: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.v2 = get_public_manifest_v2()

    def test_a_missing_runtime_node_is_refused(self) -> None:
        with self.assertRaises(PublicManifestError):
            replace(self.v2, runtime_nodes=self.v2.runtime_nodes[:-1])

    def test_a_reordered_runtime_node_set_is_refused(self) -> None:
        reordered = (self.v2.runtime_nodes[1], self.v2.runtime_nodes[0], *self.v2.runtime_nodes[2:])
        with self.assertRaises(PublicManifestError):
            replace(self.v2, runtime_nodes=reordered)

    def test_a_runtime_node_naming_an_unknown_id_is_refused(self) -> None:
        changed = (
            replace(self.v2.runtime_nodes[0], node_id="comfyui_h3_context.H3Context.NotANode"),
            *self.v2.runtime_nodes[1:],
        )
        with self.assertRaises(PublicManifestError):
            replace(self.v2, runtime_nodes=changed)

    def test_a_duplicate_fixture_id_is_refused(self) -> None:
        with self.assertRaises(PublicManifestError):
            replace(
                self.v2,
                workflow_fixtures=(self.v2.workflow_fixtures[0], self.v2.workflow_fixtures[0]),
            )

    def test_a_host_core_type_may_not_also_be_a_repository_node(self) -> None:
        with self.assertRaises(PublicManifestError):
            replace(
                self.v2,
                host_core_node_types=tuple(sorted((*HOST_CORE_NODE_TYPES, self.v2.node_ids[0]))),
            )

    def test_the_two_class_facts_come_from_the_registered_classes(self) -> None:
        """`function` and `output_node` are the only fields v2 cannot check against a contract.

        Nothing downstream re-derives them -- the v1 aggregate has no contract field to compare them
        with -- so the guarantee has to be that the adapter reads them off the class rather than
        that a later validator catches a wrong one.
        """

        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
        from comfyui_h3_context.public_api import collect_node_object_info

        observed = {
            item.node_id: (item.function, item.output_node)
            for item in collect_node_object_info(NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS)
        }
        for fact in self.v2.runtime_nodes:
            with self.subTest(node=fact.node_id):
                self.assertEqual((fact.function, fact.output_node), observed[fact.node_id])

    def test_a_registry_the_runtime_facts_do_not_cover_is_refused_before_derivation(self) -> None:
        """The aggregate catches it first, which is stronger than the derivation catching it."""

        from comfyui_h3_context.core.capability_manifest import build_default_binding_manifest
        from comfyui_h3_context.core.errors import CapabilityManifestError

        short = replace(self.v2.contracts, definitions=self.v2.contracts.definitions[:-1])
        with self.assertRaises(PublicManifestError):
            replace(self.v2, contracts=short)
        with self.assertRaises(CapabilityManifestError):
            build_default_binding_manifest(registry=short, registration_ids=self.v2.node_ids)
        derived = derive_binding_manifest(self.v2)
        self.assertEqual(derived.node_ids, self.v2.node_ids)

    def test_the_projection_refuses_a_fixture_with_no_inventory(self) -> None:
        with self.assertRaises(PublicManifestError):
            project_public_manifest_v1(self.v2, fixture_node_ids={})

    def test_an_unsupported_schema_is_refused(self) -> None:
        with self.assertRaises(PublicManifestError):
            PublicManifestV2(
                contracts=self.v2.contracts,
                capabilities=self.v2.capabilities,
                runtime_nodes=self.v2.runtime_nodes,
                workflow_fixtures=self.v2.workflow_fixtures,
                schema="h3.context.public.manifest.v3",
            )


class FixtureInventoryTests(unittest.TestCase):
    """The node inventory belongs to the fixture file, and unknown node types fail closed."""

    v2: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.v2 = get_public_manifest_v2()

    def test_every_declared_inventory_matches_the_file_it_names(self) -> None:
        """20 fixtures, 151 declared node ids, re-extracted from the committed documents."""

        repository = self.v2.repository_node_ids
        checked = 0
        for descriptor in self.v2.workflow_fixtures:
            with self.subTest(fixture=descriptor.fixture_id):
                path = ROOT / descriptor.path
                self.assertTrue(path.is_file(), descriptor.path)
                document = json.loads(path.read_text(encoding="utf-8"))
                inventory = extract_fixture_node_types(
                    document, descriptor.fixture_id, repository_node_ids=repository
                ).require_known()
                declared = DEFAULT_FIXTURE_NODE_INVENTORY[descriptor.fixture_id]
                self.assertEqual(inventory.repository_nodes, tuple(sorted(declared)))
                checked += 1
        self.assertEqual(checked, len(DEFAULT_WORKFLOW_FIXTURES))

    def test_every_host_core_type_the_fixtures_use_is_declared(self) -> None:
        repository = self.v2.repository_node_ids
        seen: set[str] = set()
        for descriptor in self.v2.workflow_fixtures:
            document = json.loads((ROOT / descriptor.path).read_text(encoding="utf-8"))
            inventory = extract_fixture_node_types(
                document, descriptor.fixture_id, repository_node_ids=repository
            )
            self.assertEqual(inventory.unknown_nodes, ())
            seen.update(inventory.host_core_nodes)
        self.assertTrue(seen)
        self.assertTrue(seen.issubset(set(HOST_CORE_NODE_TYPES)))

    def test_the_declared_native_anchors_track_the_pinned_host_contract(self) -> None:
        """The two anchors are pinned elsewhere; this fails if the declaration drifts from them."""

        self.assertIn(NATIVE_H3_IMAGE_NODE_ID, HOST_CORE_NODE_TYPES)
        self.assertIn(NATIVE_H3_REFERENCE_NODE_ID, HOST_CORE_NODE_TYPES)

    def test_an_unknown_node_type_fails_closed(self) -> None:
        document = {"prompt": {"1": {"class_type": "SomeoneElsesNode", "inputs": {}}}}
        inventory = extract_fixture_node_types(
            document, "probe.unknown", repository_node_ids=self.v2.repository_node_ids
        )
        self.assertEqual(inventory.unknown_nodes, ("SomeoneElsesNode",))
        with self.assertRaises(PublicManifestError):
            inventory.require_known()

    def test_a_host_core_node_alone_is_not_a_failure(self) -> None:
        document = {"prompt": {"1": {"class_type": "LoadImage", "inputs": {}}}}
        inventory = extract_fixture_node_types(
            document, "probe.core", repository_node_ids=self.v2.repository_node_ids
        ).require_known()
        self.assertEqual(inventory.host_core_nodes, ("LoadImage",))
        self.assertEqual(inventory.repository_nodes, ())

    def test_socket_types_are_not_mistaken_for_node_types(self) -> None:
        """litegraph spells socket types `type` too; a generic walk reports STRING as a node."""

        document = {
            "nodes": [
                {
                    "type": "comfyui_h3_context.H3Context.Request",
                    "inputs": [{"name": "task_mode", "type": "STRING"}],
                    "outputs": [{"name": "report", "type": "H3_CONTEXT_REPORT"}],
                }
            ]
        }
        inventory = extract_fixture_node_types(
            document, "probe.sockets", repository_node_ids=self.v2.repository_node_ids
        ).require_known()
        self.assertEqual(inventory.repository_nodes, ("comfyui_h3_context.H3Context.Request",))
        self.assertEqual(inventory.host_core_nodes, ())

    def test_a_subgraph_instance_is_not_a_node_type(self) -> None:
        document = {
            "nodes": [{"type": "9d55db75-2dd4-5f16-8cdd-2a5b7e8fd1b4"}],
            "definitions": {
                "subgraphs": [
                    {
                        "id": "9d55db75-2dd4-5f16-8cdd-2a5b7e8fd1b4",
                        "nodes": [{"type": "comfyui_h3_context.H3Context.Plan"}],
                    }
                ]
            },
        }
        inventory = extract_fixture_node_types(
            document, "probe.subgraph", repository_node_ids=self.v2.repository_node_ids
        ).require_known()
        self.assertEqual(inventory.repository_nodes, ("comfyui_h3_context.H3Context.Plan",))

    def test_a_document_carrying_both_shapes_is_refused(self) -> None:
        """The one shape where a node could pass `require_known()` without being looked at.

        The API-prompt and litegraph branches are exclusive, so a document with both would have its
        node list read by nobody -- not classified repository, not host-core, not even unknown. That
        is silence where the module promises a refusal, so an ambiguous document is refused instead.
        """

        document = {
            "prompt": {"1": {"class_type": "comfyui_h3_context.H3Context.Request", "inputs": {}}},
            "nodes": [{"type": "SomeoneElsesUnknownNode"}],
        }
        with self.assertRaises(PublicManifestError) as raised:
            extract_fixture_node_types(
                document, "probe.combo", repository_node_ids=self.v2.repository_node_ids
            )
        self.assertIn("both an API prompt and a litegraph node list", str(raised.exception))

    def test_no_shipped_fixture_carries_both_shapes(self) -> None:
        for descriptor in self.v2.workflow_fixtures:
            with self.subTest(fixture=descriptor.fixture_id):
                document = json.loads((ROOT / descriptor.path).read_text(encoding="utf-8"))
                self.assertFalse(
                    isinstance(document.get("prompt"), dict)
                    and isinstance(document.get("nodes"), list)
                )

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        with self.assertRaises(PublicManifestError):
            extract_fixture_node_types([], "probe.bad", repository_node_ids=frozenset())


class DeclaredSurfaceTests(unittest.TestCase):
    """Bounds, ordering and the content-free rule, on both members and the wire."""

    v2: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.v2 = get_public_manifest_v2()

    def test_the_host_core_set_is_sorted_unique_and_bounded(self) -> None:
        self.assertEqual(list(HOST_CORE_NODE_TYPES), sorted(set(HOST_CORE_NODE_TYPES)))
        self.assertLessEqual(len(HOST_CORE_NODE_TYPES), 32)

    def test_an_unsorted_host_core_set_is_refused(self) -> None:
        with self.assertRaises(PublicManifestError):
            replace(self.v2, host_core_node_types=tuple(reversed(HOST_CORE_NODE_TYPES)))

    def test_a_fixture_path_may_not_traverse_or_be_absolute(self) -> None:
        base = self.v2.workflow_fixtures[0]
        for path in ("../secrets/thing.json", "workflows/../../thing.json"):
            with self.subTest(path=path), self.assertRaises(PublicManifestError):
                WorkflowFixtureDescriptor(
                    fixture_id=base.fixture_id,
                    path=path,
                    kind=base.kind,
                    task_modes=base.task_modes,
                )

    def test_runtime_facts_reject_a_malformed_function_or_flag(self) -> None:
        for function in ("", "with space", "has/slash", "9leading", "x" * 65):
            with self.subTest(function=function), self.assertRaises(PublicManifestError):
                RuntimeNodeFacts("comfyui_h3_context.H3Context.Request", function, False)
        with self.assertRaises(PublicManifestError):
            RuntimeNodeFacts("comfyui_h3_context.H3Context.Request", "execute", "no")  # type: ignore[arg-type]

    def test_v2_is_not_stricter_about_function_than_v1(self) -> None:
        """A node class with a CamelCase FUNCTION must not build under v1 and fail under v2."""

        from comfyui_h3_context.core.public_manifest import NodeObjectInfo

        accepted = RuntimeNodeFacts("comfyui_h3_context.H3Context.Request", "Execute", False)
        self.assertEqual(accepted.function, "Execute")
        NodeObjectInfo(
            node_id="comfyui_h3_context.H3Context.Request",
            display_name="H3 Context Request",
            category="h3_context/contracts",
            required_inputs=(),
            optional_inputs=(),
            output_types=(),
            function="Execute",
            output_node=False,
        )

    def test_a_task_mode_list_is_bounded_without_narrowing_v1(self) -> None:
        """The ceiling sits above the number of modes that exist, so it cannot refuse valid data."""

        from comfyui_h3_context.core.contracts import TaskMode
        from comfyui_h3_context.core.public_manifest_v2 import MAX_FIXTURE_TASK_MODES

        self.assertGreater(MAX_FIXTURE_TASK_MODES, len(TaskMode))
        base = self.v2.workflow_fixtures[0]
        with self.assertRaises(PublicManifestError):
            WorkflowFixtureDescriptor(
                fixture_id=base.fixture_id,
                path=base.path,
                kind=base.kind,
                task_modes=tuple([TaskMode.T2VA] * (MAX_FIXTURE_TASK_MODES + 1)),
            )

    def test_build_requires_the_adapter_to_supply_runtime_facts(self) -> None:
        """The two class facts cannot be invented by the pure core, so it refuses to guess them."""

        with self.assertRaises(PublicManifestError):
            build_public_manifest_v2()

    def test_the_v2_adapter_still_catches_a_drift_v1_would_have_caught(self) -> None:
        """v2 derives object-info from the contract, so the comparison v1 made has to move here.

        In v1 the registered display name, category, input names and output types travel into
        object-info and `PublicManifest._validate_object_info` compares them against the contract.
        Deriving those fields instead would make that comparison trivially true -- the drift would
        stop being detected rather than stop existing -- so the adapter checks them directly.
        """

        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

        first = next(iter(NODE_CLASS_MAPPINGS))
        drifted = dict(NODE_DISPLAY_NAME_MAPPINGS)
        drifted[first] = "A Name The Contract Does Not Declare"
        with self.assertRaises(PublicManifestError) as raised:
            build_runtime_public_manifest_v2(display_names=drifted)
        self.assertIn("display name drift", str(raised.exception))

    def test_the_v2_adapter_still_catches_a_missing_registration(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

        short = dict(NODE_CLASS_MAPPINGS)
        short.pop(next(iter(short)))
        with self.assertRaises(PublicManifestError):
            build_runtime_public_manifest_v2(
                node_mapping=short, display_names=NODE_DISPLAY_NAME_MAPPINGS
            )

    def test_the_wire_carries_no_content_path_or_credential(self) -> None:
        text = _canonical(self.v2.to_wire()).casefold()
        for marker in ("http://", "https://", "authorization", "bearer ", "api_key", "password"):
            self.assertNotIn(marker, text)
        self.assertNotIn("b:\\", text)
        self.assertNotIn("c:\\", text)

    def test_the_fingerprint_answers_to_the_declared_surface(self) -> None:
        self.assertRegex(self.v2.fingerprint, r"^sha256:[0-9a-f]{64}$")
        changed = replace(
            self.v2,
            runtime_nodes=(
                replace(
                    self.v2.runtime_nodes[0], output_node=not self.v2.runtime_nodes[0].output_node
                ),
                *self.v2.runtime_nodes[1:],
            ),
        )
        self.assertNotEqual(changed.fingerprint, self.v2.fingerprint)


if __name__ == "__main__":  # pragma: no cover - convenience for a single-file run
    unittest.main()
