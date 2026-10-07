"""M18-03 cross-language structural contract authority tests.

The claim this item makes is narrow and checkable: for every frozen cross-language wire there is
exactly one place that decides its key set -- the Python ``to_wire`` that produces it -- and the
browser reads that decision rather than restating it. So the tests here are about the ways that
claim could quietly stop being true.

Three of them carry the weight. The record must re-extract from the live source, or it becomes a
snapshot of a shape the code has since changed. Extraction must fail closed, because a key set
guessed from half a ``to_wire`` builds a closed-shape check that rejects payloads the producer
legitimately emits. And no codec may re-declare a shape the table owns, because a duplicate that
comes back is the whole problem returning.

The rest fix the reviewable properties: one order, one fingerprint, bytes that match a fresh
generation, and a record that carries key names and nothing else.

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

from comfyui_h3_context.core.contract_inventory import FORBIDDEN_RECORD_TEXT
from scripts.governance.cross_language_surface import (
    CROSS_LANGUAGE_SURFACE_SCHEMA,
    CrossLanguageSurface,
    CrossLanguageSurfaceError,
    WireShape,
    build_cross_language_surface,
    extract_module_shapes,
    extract_shape,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_unfinished_inline_array_does_not_stall_shape_validation() -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scripts.cross_language_surface import _TS_INLINE_ARRAY; "
            'assert _TS_INLINE_ARRAY.findall(\'["value"\' + " " * 2000 + "0]") == []; '
            'assert _TS_INLINE_ARRAY.findall(\'["alpha", "beta",]\') == [\'"alpha", "beta",\']',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert child.returncode == 0, child.stderr


# The artifact stays in the package because `frontend/src` compiles it in; its schema and
# the shipped schemas read below have no runtime or bundle reader and moved with M23-55.
CONTRACTS = REPO_ROOT / "comfyui_h3_context" / "contracts"
GOVERNANCE = REPO_ROOT / "governance" / "contracts"
ARTIFACT = CONTRACTS / "cross_language_surface_v1.json"
ARTIFACT_SCHEMA = GOVERNANCE / "cross_language_surface_v1.schema.json"
GENERATED_TS = REPO_ROOT / "frontend" / "src" / "contracts" / "generatedSurface.ts"


def _load_generator() -> Any:
    """Import the generator by path; `scripts/` is a directory, not an importable package."""

    name = "m18_03_cross_language_surface_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "cross_language_surface.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()

#: The five declarations that also have a JSON Schema.  They are the only places where a third,
#: independent statement of the same shape exists, so they are where "the authority agrees with the
#: schema" can be asserted rather than assumed.  `productShellProjectionKeys` is the pilot: it is
#: declared three times -- Python, schema, codec -- and all three must name the same 19 keys.
SCHEMA_BACKED: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("productShellProjectionKeys", "product_shell_v1.schema.json", ()),
    ("productShellBindingKeys", "product_shell_v1.schema.json", ("$defs", "binding")),
    ("productShellHostProfileKeys", "product_shell_v1.schema.json", ("properties", "host")),
    ("sidebarWorkspaceProjectionKeys", "sidebar_workspace_v1.schema.json", ()),
    ("executionCorrelationKeys", "sidebar_workspace_v1.schema.json", ("$defs", "correlation")),
)


def _shape(**overrides: object) -> WireShape:
    """A minimal well-formed shape; each test names only the facet it is about."""

    base: dict[str, object] = {
        "export_name": "exampleWireKeys",
        "module_path": "comfyui_h3_context/core/example.py",
        "class_name": "ExampleWire",
        "required_keys": ("alpha", "beta"),
    }
    base.update(overrides)
    return WireShape(**base)  # type: ignore[arg-type]


class ExtractionTests(unittest.TestCase):
    """What the parser may conclude from a module, and what it must refuse to conclude."""

    def test_a_literal_return_resolves_to_its_key_set(self) -> None:
        source = (
            "class Example:\n"
            "    def to_wire(self):\n"
            '        return {"beta": self.b, "alpha": self.a}\n'
        )
        shape = extract_module_shapes(source, "comfyui_h3_context/core/example.py")["Example"]
        self.assertEqual(shape.required_keys, ("alpha", "beta"))
        self.assertEqual(shape.optional_keys, ())
        self.assertEqual(shape.unresolved, ())

    def test_a_conditional_key_is_optional_not_required(self) -> None:
        """A key emitted only sometimes must not become a key the browser demands."""

        source = (
            "class Example:\n"
            "    def to_wire(self):\n"
            '        wire = {"alpha": self.a}\n'
            "        if self.b is not None:\n"
            '            wire["beta"] = self.b\n'
            "        return wire\n"
        )
        shape = extract_module_shapes(source, "comfyui_h3_context/core/example.py")["Example"]
        self.assertEqual(shape.required_keys, ("alpha",))
        self.assertEqual(shape.optional_keys, ("beta",))
        self.assertEqual(shape.unresolved, ())

    def test_a_non_literal_key_is_unresolved_rather_than_dropped(self) -> None:
        source = (
            "class Example:\n"
            "    def to_wire(self):\n"
            '        return {"alpha": self.a, self.name: self.b}\n'
        )
        shape = extract_module_shapes(source, "comfyui_h3_context/core/example.py")["Example"]
        self.assertTrue(shape.unresolved)
        self.assertIn("alpha", shape.required_keys)

    def test_a_comprehension_and_a_computed_subscript_are_unresolved(self) -> None:
        for body in (
            "        return {key: 1 for key in self.names}\n",
            '        wire = {"alpha": self.a}\n        wire[self.name] = 1\n        return wire\n',
        ):
            with self.subTest(body=body.strip()[:24]):
                source = "class Example:\n    def to_wire(self):\n" + body
                shape = extract_module_shapes(source, "comfyui_h3_context/core/example.py")
                self.assertTrue(shape["Example"].unresolved)

    def test_extract_shape_refuses_an_unresolved_or_absent_declaration(self) -> None:
        """Fail closed. A partial key set is worse than none, so it must never be returned."""

        unresolved = REPO_ROOT / "tests" / "fixtures" / "_m18_03_unresolved_wire.py"
        unresolved.write_text(
            "class Example:\n"
            "    def to_wire(self):\n"
            '        return {"alpha": self.a, self.name: self.b}\n',
            encoding="utf-8",
        )
        self.addCleanup(unresolved.unlink)
        relative = "tests/fixtures/_m18_03_unresolved_wire.py"
        with self.assertRaises(CrossLanguageSurfaceError) as raised:
            extract_shape(unresolved, relative, "Example")
        self.assertIn("unresolved", str(raised.exception))
        with self.assertRaises(CrossLanguageSurfaceError):
            extract_shape(unresolved, relative, "Missing")

    def test_extraction_reads_the_module_without_importing_it(self) -> None:
        """The parser must describe code it would be unsafe to run; a marker proves it did not."""

        marker = REPO_ROOT / "tests" / "fixtures" / "_m18_03_side_effect_marker.txt"
        module = REPO_ROOT / "tests" / "fixtures" / "_m18_03_side_effect_wire.py"
        module.write_text(
            "import pathlib\n"
            f"pathlib.Path({str(marker.name)!r}).write_text('ran', encoding='utf-8')\n"
            "\n"
            "class Example:\n"
            "    def to_wire(self):\n"
            '        return {"alpha": self.a}\n',
            encoding="utf-8",
        )
        self.addCleanup(module.unlink)
        shape = extract_shape(module, "tests/fixtures/_m18_03_side_effect_wire.py", "Example")
        self.assertEqual(shape.required_keys, ("alpha",))
        self.assertFalse(marker.exists())
        self.assertFalse((Path.cwd() / marker.name).exists())


class ShapeGuardTests(unittest.TestCase):
    """A record that accepts a malformed shape cannot be the authority for anything."""

    def test_a_well_formed_shape_is_accepted(self) -> None:
        shape = _shape()
        self.assertEqual(shape.all_keys, ("alpha", "beta"))
        self.assertNotIn("optional_keys", shape.to_wire())

    def test_unsorted_or_duplicated_keys_are_refused(self) -> None:
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(required_keys=("beta", "alpha"))
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(required_keys=("alpha", "alpha"))

    def test_a_key_cannot_be_both_required_and_optional(self) -> None:
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(required_keys=("alpha",), optional_keys=("alpha",))

    def test_an_absolute_or_traversing_module_path_is_refused(self) -> None:
        for path in ("/etc/passwd", "../secrets/example.py", "comfyui_h3_context/../../example.py"):
            with self.subTest(path=path), self.assertRaises(CrossLanguageSurfaceError):
                _shape(module_path=path)

    def test_names_outside_their_convention_are_refused(self) -> None:
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(export_name="ExampleWireKeys")
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(class_name="example_wire")
        with self.assertRaises(CrossLanguageSurfaceError):
            _shape(required_keys=("Alpha",))

    def test_a_shape_may_not_be_declared_twice(self) -> None:
        first = _shape()
        second = _shape(class_name="OtherWire")
        with self.assertRaises(CrossLanguageSurfaceError):
            build_cross_language_surface((first, second))

    def test_the_surface_is_sorted_for_the_caller_but_stays_sorted_afterwards(self) -> None:
        surface = build_cross_language_surface(
            (_shape(export_name="zetaKeys"), _shape(export_name="alphaKeys"))
        )
        self.assertEqual([item.export_name for item in surface.shapes], ["alphaKeys", "zetaKeys"])
        with self.assertRaises(CrossLanguageSurfaceError):
            CrossLanguageSurface(shapes=tuple(reversed(surface.shapes)))

    def test_the_fingerprint_answers_to_the_key_set_not_to_input_order(self) -> None:
        ordered = build_cross_language_surface((_shape(export_name="alphaKeys"),))
        same = build_cross_language_surface((_shape(export_name="alphaKeys"),))
        self.assertEqual(ordered.fingerprint, same.fingerprint)
        changed = build_cross_language_surface(
            (_shape(export_name="alphaKeys", required_keys=("alpha", "beta", "gamma")),)
        )
        self.assertNotEqual(ordered.fingerprint, changed.fingerprint)

    def test_an_unsupported_schema_is_refused(self) -> None:
        with self.assertRaises(CrossLanguageSurfaceError):
            CrossLanguageSurface(shapes=(_shape(),), schema="h3-context-cross-language-surface/2")


class GeneratedArtifactTests(unittest.TestCase):
    """The committed record, the code it claims to describe, and the module the browser reads."""

    document: dict[str, Any]
    surface: CrossLanguageSurface

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        cls.surface = GENERATOR.build_surface()

    def test_the_artifact_regenerates_byte_identically(self) -> None:
        self.assertEqual(
            GENERATOR.artifact_bytes(self.surface), ARTIFACT.read_bytes().replace(b"\r\n", b"\n")
        )

    def test_the_generated_typescript_reproduces_the_artifact(self) -> None:
        committed = GENERATED_TS.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(GENERATOR.typescript_module(self.surface), committed)

    def test_the_record_and_its_fingerprint_agree(self) -> None:
        self.assertEqual(self.document["schema"], CROSS_LANGUAGE_SURFACE_SCHEMA)
        self.assertEqual(self.document["fingerprint"], self.surface.fingerprint)
        exports = [shape["export_name"] for shape in self.document["shapes"]]
        self.assertEqual(exports, sorted(exports))
        self.assertEqual(len(set(exports)), len(exports))

    def test_every_recorded_shape_re_extracts_from_its_named_authority(self) -> None:
        """The record must track the code. A snapshot that drifts is worse than no record."""

        for shape in self.document["shapes"]:
            with self.subTest(export=shape["export_name"]):
                extracted = extract_shape(
                    REPO_ROOT / shape["module_path"], shape["module_path"], shape["class_name"]
                )
                self.assertEqual(list(extracted.required_keys), shape["required_keys"])
                self.assertEqual(list(extracted.optional_keys), shape.get("optional_keys", []))

    def test_each_shape_names_exactly_one_authority(self) -> None:
        """AC-M18-03-01: one shape, one module and class. Two authorities is an unowned wire."""

        authorities = [
            (shape["module_path"], shape["class_name"]) for shape in self.document["shapes"]
        ]
        self.assertEqual(len(set(authorities)), len(authorities))
        for module_path, _ in authorities:
            self.assertTrue((REPO_ROOT / module_path).is_file(), module_path)

    def test_the_schema_backed_declarations_agree_with_the_authority(self) -> None:
        """Where a third statement of a shape exists, all three must name the same keys."""

        for export, schema_name, pointer in SCHEMA_BACKED:
            with self.subTest(export=export):
                node: Any = json.loads((GOVERNANCE / schema_name).read_text(encoding="utf-8"))
                for part in pointer:
                    node = node[part]
                shape = self.surface.shape(export)
                self.assertEqual(sorted(node["properties"]), list(shape.all_keys))
                self.assertIs(node.get("additionalProperties"), False)

    def test_the_artifact_validates_against_its_schema(self) -> None:
        import jsonschema

        schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(self.document))
        self.assertEqual([error.message for error in errors], [])

    def test_the_record_carries_names_and_nothing_else(self) -> None:
        """Key names, module paths and class names -- no value, locator, credential or path."""

        for text in (
            ARTIFACT.read_text(encoding="utf-8"),
            GENERATED_TS.read_text(encoding="utf-8"),
        ):
            match = FORBIDDEN_RECORD_TEXT.search(text)
            self.assertIsNone(match, match.group(0) if match else "")


class DuplicationTests(unittest.TestCase):
    """AC-M18-03-02: each owned whole-wire key set exists once; refinements are named."""

    def test_no_codec_re_declares_a_shape_the_table_owns(self) -> None:
        self.assertEqual(GENERATOR.duplicate_shapes(GENERATOR.build_surface()), [])

    def test_a_reintroduced_duplicate_is_detected_however_it_is_written(self) -> None:
        """The detector is only worth having if it fires; each form here was found the hard way."""

        surface = GENERATOR.build_surface()
        owned = surface.shape("executionCorrelationKeys")
        codec = Path(GENERATOR.ROOT) / GENERATOR.CODEC_DIR / "_m18_03_probe_codec.ts"
        self.addCleanup(codec.unlink)
        named = ", ".join(f'"{key}"' for key in owned.all_keys)
        joined = ",".join(owned.all_keys)
        forms = {
            "a named const": f"const probeKeys = [{named}] as const;\n",
            "an annotated const": f"const probeKeys: readonly string[] = [{named}];\n",
            "an argument literal": f"closed(value, [{named}], 'probe');\n",
            "a joined string": f'closed(value, "{joined}");\n',
        }
        for label, source in forms.items():
            with self.subTest(form=label):
                codec.write_text(source, encoding="utf-8")
                reported = GENERATOR.duplicate_shapes(surface)
                self.assertTrue(reported, f"{label} went undetected")
                for entry in reported:
                    self.assertIn(owned.export_name, entry)
                    self.assertIn(codec.name, entry)

    def test_every_refinement_is_recorded_with_a_reason_and_stays_handwritten(self) -> None:
        """What must not be assimilated: sub-selections, request shapes, a composed shape."""

        owned = {shape.export_name for shape in GENERATOR.build_surface().shapes}
        declared = {
            f"{codec}::{name}"
            for codec, lists in GENERATOR.codec_key_lists().items()
            for name in lists
        }
        self.assertEqual(declared, set(GENERATOR.HANDWRITTEN_REFINEMENTS))
        for qualified, reason in GENERATOR.HANDWRITTEN_REFINEMENTS.items():
            with self.subTest(refinement=qualified):
                codec, _, name = qualified.partition("::")
                self.assertTrue((Path(GENERATOR.ROOT) / GENERATOR.CODEC_DIR / codec).is_file())
                self.assertNotIn(name, owned)
                self.assertGreaterEqual(len(reason), 24)

    def test_a_refinement_exemption_does_not_leak_between_codecs(self) -> None:
        """Two codecs declare an unrelated `actionKeys`; a bare-name exemption would cover both."""

        qualified = sorted(GENERATOR.HANDWRITTEN_REFINEMENTS)
        self.assertIn("semanticProposalReviewCodec.ts::actionKeys", qualified)
        self.assertIn("transactionTransparencyCodec.ts::actionKeys", qualified)
        for entry in qualified:
            self.assertIn("::", entry)

    def test_the_export_table_is_frozen_and_complete(self) -> None:
        exports = [GENERATOR.export_name(entry.class_name) for entry in GENERATOR.DECLARED]
        self.assertEqual(len(set(exports)), len(exports))
        self.assertEqual(
            sorted(exports),
            [shape.export_name for shape in GENERATOR.build_surface().shapes],
        )


if __name__ == "__main__":  # pragma: no cover - convenience for a single-file run
    unittest.main()
