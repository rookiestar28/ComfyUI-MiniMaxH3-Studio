"""Build the committed edge vectors that pin Python and browser canonical string identity.

`comfyui_h3_context/core/canonical.py` and `frontend/src/contracts/canonicalFingerprint.ts` compute
the same identity for the same prompt text, and exactly one place in the repository depends on that
being true: `frontend/src/host/sidebarHost.ts` fingerprints the workspace prompt in the browser and
compares it against the backend-produced value.  Two runtimes agreeing "in principle" is not
evidence, so the agreement is pinned to a committed file that both test suites read.

Every vector is expressed as **code points**, never as literal text.  Three reasons, all of them
things that went wrong or would have:

* A lone surrogate cannot survive a UTF-8 round trip, so a vector for one cannot be stored as text.
* Escape sequences written through a shell heredoc are corrupted before they reach the file.
* A reviewer can see `[101, 769]` and know exactly which characters a vector is about, where a
  normalized literal looks identical to its precomposed twin on screen.

The fixture records only what **Python** measures.  The browser side is asserted by the frontend
test importing the shipped module and checking two properties against these rows -- that it accepts
exactly the vectors Python accepts, and that it produces exactly the same digest where both accept.
Recording an expected browser disposition here would be a restatement of the browser guard, and a
restatement is what produced a wrong divergence measurement during planning.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.canonical import (  # noqa: E402
    MAX_CANONICAL_STRING_LENGTH,
    canonical_fingerprint,
)
from comfyui_h3_context.core.errors import CanonicalizationError  # noqa: E402

ARTIFACT_PATH = Path("tests/fixtures/canonical_cross_language_vectors.json")
VECTOR_SCHEMA = "h3-context-canonical-cross-language-vectors/1"
LIMIT = MAX_CANONICAL_STRING_LENGTH


@dataclass(frozen=True)
class _Segment:
    """One run of code points, optionally repeated, so a limit vector stays small on disk."""

    code_points: tuple[int, ...]
    repeat: int = 1

    def text(self) -> str:
        return "".join(chr(item) for item in self.code_points) * self.repeat

    def to_wire(self) -> dict[str, object]:
        wire: dict[str, object] = {"code_points": list(self.code_points)}
        if self.repeat != 1:
            wire["repeat"] = self.repeat
        return wire


@dataclass(frozen=True)
class _Vector:
    name: str
    description: str
    segments: tuple[_Segment, ...]

    def text(self) -> str:
        return "".join(segment.text() for segment in self.segments)


def _run(name: str, description: str, *code_points: int) -> _Vector:
    return _Vector(name, description, (_Segment(tuple(code_points)),))


def _repeated(name: str, description: str, code_points: Sequence[int], repeat: int) -> _Vector:
    return _Vector(name, description, (_Segment(tuple(code_points), repeat),))


def build_vectors() -> tuple[_Vector, ...]:
    """The frozen edge set: normalization, escaping, unsafe code points and both length units.

    ONE INPUT IS NOT EXPRESSIBLE HERE, and finding that out is part of what these vectors are for.
    A high surrogate immediately followed by a low surrogate is *two lone surrogates* in Python and
    *one astral character* in JavaScript -- ``String.fromCodePoint(0xD83D, 0xDE00)`` is U+1F600,
    because a JavaScript string is a UTF-16 code-unit sequence and an adjacent well-formed pair is
    that character by definition.  So the two runtimes would be handed different inputs and would
    correctly disagree about them: Python refuses two surrogates, the browser accepts an emoji.  A
    vector like that measures the encodings, not the canonicalizers, and it is left out for that
    reason.  ``reversed_surrogates`` covers what the vector was reaching for -- a surrogate sequence
    that cannot pair -- and stays the same input in both runtimes.
    """

    return (
        # --- plain text and JSON escaping, where the two runtimes must simply agree ---
        _Vector("empty", "the empty string still has an identity", (_Segment(()),)),
        _run(
            "ascii_hello",
            "the shortest agreement already asserted by the browser suite",
            *b"hello",
        ),
        _Vector(
            "ascii_prompt_with_label",
            "a realistic prompt carrying a native reference label",
            (_Segment(tuple(ord(c) for c in "Subject: A safe prompt with <Picture 1>.")),),
        ),
        _run("json_quote", "a quotation mark must be escaped identically by both encoders", 0x22),
        _run("json_backslash", "a backslash must be escaped identically by both encoders", 0x5C),
        _run("json_solidus", "neither encoder escapes a forward solidus", 0x2F),
        _run("control_tab", "a short escape rather than a numeric one", 0x09),
        _run("control_line_feed", "a short escape rather than a numeric one", 0x0A),
        _run("control_carriage_return", "a short escape rather than a numeric one", 0x0D),
        _run("control_backspace", "a short escape rather than a numeric one", 0x08),
        _run("control_form_feed", "a short escape rather than a numeric one", 0x0C),
        _run("control_unit_separator", "a numeric escape below the short-escape set", 0x1F),
        _run("control_delete", "U+007F is above the escaping threshold and stays literal", 0x7F),
        _run("control_c1_next_line", "a C1 control is not escaped by either encoder", 0x85),
        _run("nul", "the code point both runtimes refuse outright", 0x00),
        # --- NFC: the normalization both sides apply before hashing ---
        _run("nfc_precomposed_e_acute", "the composed form is already normal", 0xE9),
        _run("nfc_decomposed_e_acute", "must fold onto the composed form", 0x65, 0x301),
        _run(
            "nfc_reordered_combining_marks",
            "canonical ordering must reorder cedilla before acute",
            0x65,
            0x301,
            0x327,
        ),
        _run("nfc_singleton_angstrom", "a singleton decomposition folds to U+00C5", 0x212B),
        _run("nfc_singleton_ohm", "a singleton decomposition folds to U+03A9", 0x2126),
        _run("nfc_hangul_jamo", "conjoining jamo must compose to U+AC01", 0x1100, 0x1161, 0x11A8),
        _run("nfc_hangul_precomposed", "the composed syllable the jamo vector must equal", 0xAC01),
        _run("nfc_ligature_unchanged", "a compatibility ligature is NOT folded by NFC", 0xFB01),
        _run("nfc_circled_digit_unchanged", "a compatibility digit is NOT folded by NFC", 0x2460),
        _run("nfc_combining_mark_alone", "a combining mark with nothing to combine with", 0x301),
        # --- code points that historically break one encoder or the other ---
        _run("line_separator", "U+2028 stays literal in JSON on both sides", 0x2028),
        _run("paragraph_separator", "U+2029 stays literal in JSON on both sides", 0x2029),
        _run("byte_order_mark", "U+FEFF is ordinary text here, not a signature", 0xFEFF),
        _run("zero_width_joiner", "U+200D survives normalization unchanged", 0x200D),
        _run(
            "right_to_left_override",
            "a bidi control is text, not a rendering instruction",
            0x202E,
        ),
        _run("replacement_character", "U+FFFD is a legitimate input, not a decode failure", 0xFFFD),
        _run("noncharacter_fffe", "a noncharacter is still a code point both may hash", 0xFFFE),
        _run("noncharacter_ffff", "a noncharacter is still a code point both may hash", 0xFFFF),
        # --- surrogates: both runtimes must refuse, and refuse the same inputs ---
        _run("lone_high_surrogate", "an unpaired high surrogate is refused", 0xD800),
        _run("lone_low_surrogate", "an unpaired low surrogate is refused", 0xDFFF),
        # A low surrogate BEFORE a high one, deliberately.  Written the other way round this is not
        # a shared input at all: see the note above `build_vectors`.
        _run("reversed_surrogates", "two surrogates that cannot pair are refused", 0xDE00, 0xD83D),
        # --- astral characters: one code point, two UTF-16 code units ---
        _run("astral_emoji", "a plane-1 character both runtimes accept", 0x1F600),
        _run("astral_plane2_ideograph", "a plane-2 CJK extension character", 0x2A6B2),
        _run("astral_plane16_private", "the last usable private-use code point", 0x10FFFD),
        _run(
            "emoji_zwj_sequence", "a joined sequence, not one character", 0x1F468, 0x200D, 0x1F4BB
        ),
        _run("regional_indicator_pair", "a flag is two regional indicators", 0x1F1F9, 0x1F1FC),
        # --- the length limit, measured in both units, on both sides of the boundary ---
        _repeated(
            "bmp_at_limit", "exactly the limit in code points and in UTF-16 units", (0x61,), LIMIT
        ),
        _repeated("bmp_over_limit", "one code point past the limit", (0x61,), LIMIT + 1),
        _repeated(
            "astral_at_utf16_limit",
            "half the limit in code points, exactly the limit in UTF-16 units",
            (0x1F600,),
            LIMIT // 2,
        ),
        _Vector(
            "astral_over_utf16_limit_only",
            "inside the code-point limit and outside the UTF-16 limit: the M18-02 split",
            (_Segment((0x1F600,), LIMIT // 2 + 1),),
        ),
        _Vector(
            "mixed_over_utf16_limit_by_one",
            "exactly the limit in code points, one UTF-16 unit past it",
            (_Segment((0x61,), LIMIT - 1), _Segment((0x1F600,))),
        ),
        _repeated(
            "nfc_folds_to_exactly_the_limit",
            "twice the limit before normalization, exactly the limit after it",
            (0x65, 0x301),
            LIMIT,
        ),
        _repeated(
            "nfc_folds_to_one_past_the_limit",
            "normalization shrinks it and it is still one past the limit",
            (0x65, 0x301),
            LIMIT + 1,
        ),
    )


def measure(vector: _Vector) -> dict[str, object]:
    """Record what Python does with this vector, and nothing about what the browser does."""

    wire: dict[str, object] = {
        "name": vector.name,
        "description": vector.description,
        "segments": [segment.to_wire() for segment in vector.segments],
    }
    try:
        wire["python_fingerprint"] = canonical_fingerprint(vector.text())
        wire["python_accepted"] = True
    except CanonicalizationError:
        wire["python_accepted"] = False
    return wire


def build_document() -> dict[str, object]:
    vectors = build_vectors()
    names = [item.name for item in vectors]
    if len(set(names)) != len(names):
        raise ValueError("a vector name is used twice")
    return {
        "schema": VECTOR_SCHEMA,
        "canonical_string_limit": LIMIT,
        "vectors": [measure(item) for item in vectors],
    }


def artifact_bytes(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the fixture")
    parser.add_argument("--check", action="store_true", help="fail if the fixture is stale")
    args = parser.parse_args(argv)
    try:
        document = build_document()
    except (CanonicalizationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(document)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "cross-language vector fixture is stale"},
                ensure_ascii=False,
            )
        )
        return 1
    vectors = document["vectors"]
    if not isinstance(vectors, list):
        raise TypeError("vector document is malformed")
    accepted = sum(1 for item in vectors if item.get("python_accepted"))
    print(
        json.dumps(
            {
                "status": "PASS",
                "schema": VECTOR_SCHEMA,
                "vector_count": len(vectors),
                "python_accepted": accepted,
                "python_refused": len(vectors) - accepted,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
