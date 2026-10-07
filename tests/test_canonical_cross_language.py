"""M18-02 cross-language canonical string identity, and the length unit that split it.

`frontend/src/host/sidebarHost.ts` fingerprints the workspace prompt in the browser and compares the
result against the backend-produced value. That is the only place in the repository where a Python
identity and a JavaScript identity must be equal, and its own guard comment calls the comparison
critical. This file holds the Python half of that agreement against the committed vectors; the
browser half is held by `frontend/tests/canonicalCrossLanguage.test.ts` against the same file, so
the two runtimes are pinned to one artifact rather than to each other's prose.

The split this item closed: `_reject_text` measured `len()`, which counts Unicode code points, while
the browser measures `String.length`, which counts UTF-16 code units. A string of 32,769 astral
characters is inside one limit and outside the other, so Python emitted a fingerprint the browser
refused to compute, and the comparison raised instead of comparing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unicodedata
import unittest
from pathlib import Path
from typing import Any

from comfyui_h3_context.core.canonical import (
    MAX_CANONICAL_STRING_LENGTH,
    canonical_fingerprint,
)
from comfyui_h3_context.core.errors import CanonicalizationError

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "canonical_cross_language_vectors.json"


def _load_generator() -> Any:
    name = "m18_02_cross_language_vector_generator"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "canonical_cross_language_vectors.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Any = _load_generator()


def vector_text(vector: dict[str, Any]) -> str:
    """Rebuild a vector from its code points, exactly as the frontend test does."""

    return "".join(
        "".join(chr(point) for point in segment["code_points"]) * segment.get("repeat", 1)
        for segment in vector["segments"]
    )


def utf16_units(text: str) -> int:
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


class VectorFixtureTests(unittest.TestCase):
    document: dict[str, Any]
    vectors: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.vectors = cls.document["vectors"]

    def test_the_fixture_matches_a_fresh_generation_byte_for_byte(self) -> None:
        self.assertEqual(FIXTURE.read_bytes(), GENERATOR.artifact_bytes(GENERATOR.build_document()))

    def test_the_fixture_is_pure_ascii_so_no_encoder_can_corrupt_it(self) -> None:
        """A vector for a lone surrogate could not survive being stored as text at all."""

        self.assertTrue(FIXTURE.read_bytes().isascii())

    def test_the_fixture_declares_the_limit_it_was_measured_against(self) -> None:
        self.assertEqual(self.document["canonical_string_limit"], MAX_CANONICAL_STRING_LENGTH)

    def test_every_accepted_vector_reproduces_its_recorded_fingerprint(self) -> None:
        accepted = 0
        for vector in self.vectors:
            if not vector["python_accepted"]:
                continue
            accepted += 1
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    canonical_fingerprint(vector_text(vector)), vector["python_fingerprint"]
                )
        self.assertGreater(accepted, 30)

    def test_every_refused_vector_is_still_refused(self) -> None:
        refused = 0
        for vector in self.vectors:
            if vector["python_accepted"]:
                continue
            refused += 1
            with self.subTest(vector=vector["name"]):
                self.assertNotIn("python_fingerprint", vector)
                with self.assertRaises(CanonicalizationError):
                    canonical_fingerprint(vector_text(vector))
        self.assertGreater(refused, 0)

    def test_normalization_happens_before_the_limit_is_measured(self) -> None:
        """Both runtimes measure the NFC form, so a decomposed string that folds under the limit
        is accepted even though its input length is twice the limit."""

        by_name = {vector["name"]: vector for vector in self.vectors}
        folded = by_name["nfc_folds_to_exactly_the_limit"]
        self.assertTrue(folded["python_accepted"])
        self.assertEqual(len(vector_text(folded)), 2 * MAX_CANONICAL_STRING_LENGTH)
        self.assertFalse(by_name["nfc_folds_to_one_past_the_limit"]["python_accepted"])


class LengthUnitReconciliationTests(unittest.TestCase):
    """AC-M18-02-04 and AC-M18-02-05: the split is closed, and closing it moved no byte."""

    def test_a_string_inside_both_limits_is_accepted(self) -> None:
        at_limit = chr(0x1F600) * (MAX_CANONICAL_STRING_LENGTH // 2)
        self.assertEqual(utf16_units(at_limit), MAX_CANONICAL_STRING_LENGTH)
        self.assertTrue(canonical_fingerprint(at_limit).startswith("sha256:"))

    def test_a_string_past_the_utf16_limit_alone_is_refused(self) -> None:
        over = chr(0x1F600) * (MAX_CANONICAL_STRING_LENGTH // 2 + 1)
        self.assertLessEqual(len(over), MAX_CANONICAL_STRING_LENGTH)
        self.assertGreater(utf16_units(over), MAX_CANONICAL_STRING_LENGTH)
        with self.assertRaises(CanonicalizationError) as caught:
            canonical_fingerprint(over)
        self.assertIn("UTF-16 code-unit limit", str(caught.exception))

    def test_one_astral_character_past_the_boundary_is_enough(self) -> None:
        edge = "a" * (MAX_CANONICAL_STRING_LENGTH - 1) + chr(0x1F600)
        self.assertEqual(len(edge), MAX_CANONICAL_STRING_LENGTH)
        self.assertEqual(utf16_units(edge), MAX_CANONICAL_STRING_LENGTH + 1)
        with self.assertRaises(CanonicalizationError):
            canonical_fingerprint(edge)

    def test_the_added_refusal_can_only_fire_past_the_utf16_limit(self) -> None:
        """The delta is bounded: nothing inside both limits can be refused by the new rule.

        This is the non-destructive property stated as a test rather than as an argument. Every
        vector the canonicalizer refuses is refused for a reason that predates this item -- NUL, a
        surrogate, or a code-point count past the limit -- or else it is genuinely past the UTF-16
        limit, which is exactly what the browser already refused.
        """

        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        newly_refusable = 0
        for vector in document["vectors"]:
            if vector["python_accepted"]:
                continue
            text = vector_text(vector)
            pre_existing = (
                len(text) > MAX_CANONICAL_STRING_LENGTH
                or any(
                    ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in text
                )
                # The limit is measured after NFC, so a folding vector is judged on its NFC form.
                or len(unicodedata.normalize("NFC", text)) > MAX_CANONICAL_STRING_LENGTH
            )
            with self.subTest(vector=vector["name"]):
                if not pre_existing:
                    newly_refusable += 1
                    self.assertGreater(
                        utf16_units(unicodedata.normalize("NFC", text)),
                        MAX_CANONICAL_STRING_LENGTH,
                    )
        self.assertEqual(newly_refusable, 2)

    def test_an_ordinary_prompt_keeps_the_identity_it_already_had(self) -> None:
        """A value pinned by the browser suite before this item, unchanged after it."""

        self.assertEqual(
            canonical_fingerprint("hello"),
            "sha256:5aa762ae383fbb727af3c7a36d4940a5b8c40a989452d2304fc958ff3f354e7a",
        )


if __name__ == "__main__":
    unittest.main()
