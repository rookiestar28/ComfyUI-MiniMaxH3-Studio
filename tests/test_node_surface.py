"""M19-03 node surface tests.

The record these cover exists for one reason: a 3 300-line adapter move needs a way to prove it
changed nothing a ComfyUI host can see, and before M19-03 this repository had no test that looked at
`NODE_CLASS_MAPPINGS` as a whole. `test_node_contracts` covers the declarative registry,
`test_custom_node_loader` covers that mappings are exported at all, and the per-node suites each see
one node.

So these tests do two jobs. They pin the record's refusals -- the cases where reporting equality
would be reporting something it never established -- and they assert the live surface against the
committed baseline, which is the actual safety net for the move.

No fixture here carries a prompt, a media value, a private path or a credential.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

from scripts.governance.node_surface import (
    NODE_SURFACE_SCHEMA,
    NodeRecord,
    NodeSurface,
    NodeSurfaceError,
    SocketRecord,
    SocketSection,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "governance" / "contracts" / "node_surface_v1.json"
ARTIFACT_SCHEMA = REPO_ROOT / "governance" / "contracts" / "node_surface_v1.schema.json"

#: Every module a node class may live in. The decomposition adds to this list; nothing else may.
NODE_MODULE_PREFIX = "comfyui_h3_context."


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts" / "node_surface.py"
    spec = importlib.util.spec_from_file_location("_m19_03_node_surface", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _socket(name: str = "alpha", **overrides: Any) -> SocketRecord:
    values: dict[str, Any] = {
        "name": name,
        "section": SocketSection.REQUIRED,
        "socket_type": "STRING",
    }
    values.update(overrides)
    return SocketRecord(**values)


def _node(node_id: str = "comfyui_h3_context.H3Context.Alpha", **overrides: Any) -> NodeRecord:
    values: dict[str, Any] = {
        "node_id": node_id,
        "display_name": f"display {node_id.rsplit('.', 1)[-1]}",
        "class_name": f"H3{node_id.rsplit('.', 1)[-1]}Node",
        "category": "h3_context/testing",
        "function": "execute",
        "return_types": ("STRING",),
        "return_names": ("out",),
        "sockets": (_socket(),),
    }
    values.update(overrides)
    return NodeRecord(**values)


class SocketRecordTests(unittest.TestCase):
    def test_a_socket_name_must_be_an_identifier(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _socket("not a name")

    def test_an_option_that_cannot_be_recorded_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _socket(options={"default": object()})

    def test_a_repeated_choice_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _socket(socket_type="COMBO", choices=("a", "b", "a"))

    def test_an_empty_choice_list_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _socket(socket_type="COMBO", choices=())

    def test_choice_order_survives_the_wire(self) -> None:
        """A reordered dropdown is a visible change even when the choice set is identical."""

        declared = ("zulu", "alpha", "mike")
        wire = _socket(socket_type="COMBO", choices=declared).to_wire()
        self.assertEqual(wire["choices"], list(declared))

    def test_a_float_option_is_recorded_readably_and_exactly(self) -> None:
        wire = _socket(options={"max": 149.687}).to_wire()
        self.assertEqual(wire["options"]["max"], {"float": "149.687"})  # type: ignore[index]

    def test_a_recorded_float_round_trips_to_the_identical_double(self) -> None:
        """Which is why the record needs no separate exact-bits encoding alongside it."""

        for value in (149.687, 0.01, 0.0, 86400.0, 1.0, 1.0000001, 2.2250738585072014e-308):
            with self.subTest(value=value):
                wire = _socket(options={"max": value}).to_wire()
                recorded = wire["options"]["max"]["float"]  # type: ignore[index]
                self.assertEqual(float(recorded), value)

    def test_two_close_floats_do_not_record_the_same_value(self) -> None:
        first = _socket(options={"max": 1.0}).to_wire()["options"]["max"]  # type: ignore[index]
        second = _socket(options={"max": 1.0000001}).to_wire()["options"]["max"]  # type: ignore[index]
        self.assertNotEqual(first, second)

    def test_a_float_is_distinguishable_from_a_string_that_looks_like_one(self) -> None:
        """The float key is what makes 149.687 and "149.687" different rows."""

        number = _socket(options={"max": 149.687}).to_wire()
        text = _socket(options={"max": "149.687"}).to_wire()
        self.assertNotEqual(number["options"]["max"], text["options"]["max"])  # type: ignore[index]

    def test_a_boolean_option_is_not_recorded_as_an_integer(self) -> None:
        wire = _socket(options={"multiline": True}).to_wire()
        self.assertIs(wire["options"]["multiline"], True)  # type: ignore[index]


class NodeRecordTests(unittest.TestCase):
    def test_a_node_id_outside_this_package_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _node("some_other_pack.Thing")

    def test_return_types_and_names_must_agree_in_length(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _node(return_types=("A", "B"), return_names=("only",))

    def test_a_duplicate_socket_name_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            _node(sockets=(_socket("alpha"), _socket("alpha", section=SocketSection.OPTIONAL)))


class NodeSurfaceGuardTests(unittest.TestCase):
    def test_records_must_be_sorted_by_node_id(self) -> None:
        beta = _node("comfyui_h3_context.H3Context.Beta")
        alpha = _node("comfyui_h3_context.H3Context.Alpha")
        with self.assertRaises(NodeSurfaceError):
            NodeSurface(nodes=(beta, alpha), mapping_order=(beta.node_id, alpha.node_id))

    def test_mapping_order_must_name_exactly_the_recorded_nodes(self) -> None:
        alpha = _node("comfyui_h3_context.H3Context.Alpha")
        with self.assertRaises(NodeSurfaceError) as caught:
            NodeSurface(nodes=(alpha,), mapping_order=("comfyui_h3_context.H3Context.Ghost",))
        self.assertIn("mapping order", str(caught.exception))

    def test_a_reordered_mapping_changes_the_fingerprint(self) -> None:
        """The order a host receives nodes in is part of the surface, not presentation."""

        alpha = _node("comfyui_h3_context.H3Context.Alpha")
        beta = _node("comfyui_h3_context.H3Context.Beta")
        first = NodeSurface(nodes=(alpha, beta), mapping_order=(alpha.node_id, beta.node_id))
        second = NodeSurface(nodes=(alpha, beta), mapping_order=(beta.node_id, alpha.node_id))
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_two_nodes_may_not_share_a_display_name(self) -> None:
        alpha = _node("comfyui_h3_context.H3Context.Alpha", display_name="Same")
        beta = _node("comfyui_h3_context.H3Context.Beta", display_name="Same")
        with self.assertRaises(NodeSurfaceError):
            NodeSurface(nodes=(alpha, beta), mapping_order=(alpha.node_id, beta.node_id))

    def test_two_node_ids_may_not_resolve_to_one_class(self) -> None:
        alpha = _node("comfyui_h3_context.H3Context.Alpha", class_name="H3Shared")
        beta = _node("comfyui_h3_context.H3Context.Beta", class_name="H3Shared")
        with self.assertRaises(NodeSurfaceError):
            NodeSurface(nodes=(alpha, beta), mapping_order=(alpha.node_id, beta.node_id))

    def test_an_empty_surface_records_nothing_and_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            NodeSurface(nodes=(), mapping_order=())

    def test_an_unsupported_schema_is_refused(self) -> None:
        alpha = _node()
        with self.assertRaises(NodeSurfaceError):
            NodeSurface(nodes=(alpha,), mapping_order=(alpha.node_id,), schema="other/9")


class GeneratorNormalizationTests(unittest.TestCase):
    def test_a_bare_string_declaration_is_the_hidden_input_convention(self) -> None:
        socket = GENERATOR._socket("execution_node_id", SocketSection.HIDDEN, "UNIQUE_ID")
        self.assertEqual(socket.socket_type, "UNIQUE_ID")
        self.assertIsNone(socket.choices)

    def test_an_inline_choice_tuple_is_recorded_in_order(self) -> None:
        socket = GENERATOR._socket(
            "profile", SocketSection.REQUIRED, (("zulu", "alpha"), {"default": "zulu"})
        )
        self.assertEqual(socket.socket_type, "COMBO")
        self.assertEqual(socket.choices, ("zulu", "alpha"))

    def test_a_declaration_the_record_cannot_represent_is_refused(self) -> None:
        with self.assertRaises(NodeSurfaceError):
            GENERATOR._socket("weird", SocketSection.REQUIRED, (object(), {}))

    def test_a_declaration_with_a_third_element_is_refused(self) -> None:
        """Rather than silently dropping whatever the third element meant."""

        with self.assertRaises(NodeSurfaceError):
            GENERATOR._socket("weird", SocketSection.REQUIRED, ("STRING", {}, "extra"))


class GeneratedSurfaceTests(unittest.TestCase):
    document: dict[str, Any]
    surface: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.surface = GENERATOR.build_surface()

    def test_the_artifact_regenerates_byte_identically(self) -> None:
        """This is the M19-03 safety net: the live surface still equals the committed baseline."""

        self.assertEqual(
            GENERATOR.artifact_bytes(self.surface),
            ARTIFACT.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], NODE_SURFACE_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.surface.fingerprint)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_every_registered_node_is_recorded(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        self.assertEqual(
            sorted(NODE_CLASS_MAPPINGS),
            [node["node_id"] for node in self.document["nodes"]],
        )

    def test_the_recorded_mapping_order_is_the_order_a_host_receives(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        self.assertEqual(list(NODE_CLASS_MAPPINGS), self.document["mapping_order"])

    def test_every_recorded_class_declares_the_node_id_it_is_registered_under(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        for node_id, node_class in NODE_CLASS_MAPPINGS.items():
            with self.subTest(node=node_id):
                self.assertEqual(getattr(node_class, "__h3_context_node_id__", None), node_id)


class HostObservableAttributeTests(unittest.TestCase):
    """Regression cover for the two blind spots a distinct review found in the first record.

    The first version recorded a node's sockets, returns and category and nothing else, and sorted
    sockets by name on the reasoning that a host lays inputs out from names.  Both were wrong: the
    host's node-info route reads a further set of class attributes, and it ships `input_order` built
    straight from `INPUT_TYPES()` key order, which the frontend uses to reconcile a saved workflow's
    positional widget values.  Under that record, flipping `INPUT_IS_LIST` on a live node and
    swapping two socket declarations both left the fingerprint unchanged.
    """

    #: Every attribute the host reads off a node class, with the default it assumes when absent.
    HOST_READ = {
        "INPUT_IS_LIST": False,
        "OUTPUT_IS_LIST": None,
        "OUTPUT_TOOLTIPS": None,
        "DEPRECATED": False,
        "EXPERIMENTAL": False,
        "DEV_ONLY": False,
        "API_NODE": None,
        "SEARCH_ALIASES": (),
        "ESSENTIALS_CATEGORY": None,
        "HAS_INTERMEDIATE_OUTPUT": False,
    }

    def _record_for(self, class_name: str) -> dict[str, Any]:
        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        for node in document["nodes"]:
            if node["class_name"] == class_name:
                return dict(node)
        raise AssertionError(f"{class_name} is not in the record")

    def test_a_node_that_takes_lists_says_so(self) -> None:
        """`H3ReferenceRegistryNode` sets INPUT_IS_LIST, which changes the calling convention."""

        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        live = {
            node_id: getattr(node_class, "INPUT_IS_LIST", False)
            for node_id, node_class in NODE_CLASS_MAPPINGS.items()
        }
        recorded = {node["node_id"]: node["input_is_list"] for node in self.surface_nodes()}
        self.assertEqual(live, recorded)
        self.assertTrue(any(live.values()), "no node sets INPUT_IS_LIST; this test proves nothing")

    def surface_nodes(self) -> list[dict[str, Any]]:
        return list(json.loads(ARTIFACT.read_text(encoding="utf-8"))["nodes"])

    def test_every_host_read_attribute_matches_the_live_class(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        recorded = {node["node_id"]: node for node in self.surface_nodes()}
        for node_id, node_class in NODE_CLASS_MAPPINGS.items():
            row = recorded[node_id]
            for attribute, absent in self.HOST_READ.items():
                with self.subTest(node=node_id, attribute=attribute):
                    live = getattr(node_class, attribute, absent)
                    key = attribute.lower()
                    if isinstance(absent, bool):
                        self.assertEqual(bool(live) if live else False, row.get(key, False))
                    elif attribute == "OUTPUT_IS_LIST":
                        declared = getattr(node_class, "OUTPUT_IS_LIST", None)
                        expected = (
                            [bool(flag) for flag in declared]
                            if declared is not None
                            else [False] * len(row["return_types"])
                        )
                        self.assertEqual(expected, row["output_is_list"])
                    elif live in (None, ()):
                        self.assertNotIn(key, row)
                    else:
                        self.assertIn(key, row)

    def test_execution_hooks_are_recorded_because_the_host_calls_them(self) -> None:
        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        recorded = {node["node_id"]: node for node in self.surface_nodes()}
        for node_id, node_class in NODE_CLASS_MAPPINGS.items():
            with self.subTest(node=node_id):
                self.assertEqual(
                    hasattr(node_class, "VALIDATE_INPUTS"),
                    recorded[node_id].get("validates_inputs", False),
                )
                self.assertEqual(
                    hasattr(node_class, "IS_CHANGED"),
                    recorded[node_id].get("declares_is_changed", False),
                )

    def test_the_recorded_input_order_is_the_declared_order(self) -> None:
        """Not the sorted one. The host ships this verbatim; old workflows depend on it."""

        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        recorded = {node["node_id"]: node for node in self.surface_nodes()}
        for node_id, node_class in NODE_CLASS_MAPPINGS.items():
            spec = node_class.INPUT_TYPES()  # type: ignore[attr-defined]
            declared = {section: list(entries) for section, entries in spec.items()}
            with self.subTest(node=node_id):
                self.assertEqual(declared, recorded[node_id]["input_order"])

    def test_at_least_one_node_declares_sockets_out_of_sorted_order(self) -> None:
        """Otherwise recording declaration order would be indistinguishable from sorting it."""

        from comfyui_h3_context.nodes import NODE_CLASS_MAPPINGS

        unsorted = [
            node_id
            for node_id, node_class in NODE_CLASS_MAPPINGS.items()
            for entries in node_class.INPUT_TYPES().values()  # type: ignore[attr-defined]
            if list(entries) != sorted(entries)
        ]
        self.assertTrue(unsorted, "every node happens to declare sockets alphabetically")

    @staticmethod
    def _mutate(surface: Any, target: Any, **changes: Any) -> Any:
        from scripts.governance.node_surface import NodeRecord, NodeSurface

        replaced = NodeRecord(
            **{
                **{name: getattr(target, name) for name in NodeRecord.__dataclass_fields__},
                **changes,
            }
        )
        return NodeSurface(
            nodes=tuple(replaced if record is target else record for record in surface.nodes),
            mapping_order=surface.mapping_order,
            exported_names=surface.exported_names,
        )

    def test_reordering_a_socket_declaration_changes_the_fingerprint(self) -> None:
        """Under the sorted-only record this mutation was invisible."""

        base = GENERATOR.build_surface()
        target, section, names = next(
            (record, section, list(entries))
            for record in base.nodes
            for section, entries in record.input_order
            if len(entries) > 1
        )
        names[0], names[1] = names[1], names[0]
        reordered = tuple(
            (name, tuple(names) if name == section else entries)
            for name, entries in target.input_order
        )
        mutated = self._mutate(base, target, input_order=reordered)
        self.assertNotEqual(base.fingerprint, mutated.fingerprint)

    def test_a_flipped_input_is_list_changes_the_fingerprint(self) -> None:
        """The exact mutation the first record missed."""

        base = GENERATOR.build_surface()
        target = next(record for record in base.nodes if record.input_is_list)
        mutated = self._mutate(base, target, input_is_list=not target.input_is_list)
        self.assertNotEqual(base.fingerprint, mutated.fingerprint)

    def test_marking_a_node_deprecated_changes_the_fingerprint(self) -> None:
        """Adding an attribute no node declares today was equally invisible."""

        base = GENERATOR.build_surface()
        target = base.nodes[0]
        mutated = self._mutate(base, target, deprecated=True)
        self.assertNotEqual(base.fingerprint, mutated.fingerprint)


class ExportCompatibilityTests(unittest.TestCase):
    """The decomposition promises every existing import keeps working. This is that promise."""

    def test_every_recorded_export_still_imports_from_nodes(self) -> None:
        from comfyui_h3_context import nodes

        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        recorded = document["exported_names"]
        self.assertTrue(recorded, "the record names no exports, so it proves nothing")
        for name in recorded:
            with self.subTest(export=name):
                self.assertTrue(hasattr(nodes, name), f"{name} left comfyui_h3_context.nodes")

    def test_the_recorded_exports_are_exactly_the_declared_ones(self) -> None:
        from comfyui_h3_context import nodes

        document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        self.assertEqual(sorted(set(nodes.__all__)), document["exported_names"])

    def test_the_export_list_declares_each_name_once(self) -> None:
        from comfyui_h3_context import nodes

        duplicated = sorted({name for name in nodes.__all__ if nodes.__all__.count(name) > 1})
        self.assertEqual(duplicated, [])


class HostIndependenceTests(unittest.TestCase):
    """A node adapter module may not import a host runtime, nor reach back into the aggregator.

    Two separate rules, checked here because both are structural.  The host rule is why this package
    can be imported without ComfyUI present.  The one-way rule is what keeps the adapter layer
    acyclic after the M19-03 split: `nodes.py` imports the domain modules, so a domain module
    importing `nodes` closes a cycle.  A relative `from .nodes import X` is the way that would
    actually get written, so the check has to see `level == 1` imports and not only absolute ones.
    """

    FORBIDDEN = {"comfy", "comfy_api", "comfy_extras", "folder_paths", "nodes"}
    #: Modules that compose the aggregator and are allowed to import it.
    MAY_IMPORT_NODES = {"public_api.py", "registration.py"}

    def _node_modules(self) -> list[Path]:
        package = REPO_ROOT / "comfyui_h3_context"
        return sorted(
            path
            for path in package.glob("*.py")
            if path.name == "nodes.py" or path.name.endswith("_node.py") or "_nodes" in path.name
        )

    def test_no_node_module_imports_a_host_runtime(self) -> None:
        modules = self._node_modules()
        self.assertTrue(modules, "no node modules were found to check")
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offending: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offending |= {alias.name.split(".")[0] for alias in node.names} & self.FORBIDDEN
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    offending |= {node.module.split(".")[0]} & self.FORBIDDEN
            with self.subTest(module=path.name):
                self.assertEqual(offending, set(), f"{path.name} imports {sorted(offending)}")

    def test_no_domain_module_imports_the_aggregator_back(self) -> None:
        """A relative import is how this would get written, so an absolute-only check misses it."""

        package = REPO_ROOT / "comfyui_h3_context"
        checked = 0
        for path in sorted(package.glob("*.py")):
            if path.name in {"nodes.py", "__init__.py"} | self.MAY_IMPORT_NODES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reaches_back = any(
                isinstance(node, ast.ImportFrom) and node.level > 0 and node.module == "nodes"
                for node in ast.walk(tree)
            ) or any(
                isinstance(node, ast.Import) and any(a.name == "nodes" for a in node.names)
                for node in ast.walk(tree)
            )
            checked += 1
            with self.subTest(module=path.name):
                self.assertFalse(reaches_back, f"{path.name} imports the nodes aggregator back")
        self.assertTrue(checked, "no sibling modules were checked")

    def test_the_aggregator_is_the_one_that_imports_the_domain_modules(self) -> None:
        """Establishes the direction, so the test above is checking a real constraint."""

        aggregator = REPO_ROOT / "comfyui_h3_context" / "nodes.py"
        tree = ast.parse(aggregator.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
        }
        self.assertIn("node_support", imported)
        self.assertGreaterEqual(len(imported), 8, f"nodes.py imports only {sorted(imported)}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
