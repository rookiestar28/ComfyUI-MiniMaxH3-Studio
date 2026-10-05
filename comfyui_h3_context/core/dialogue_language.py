"""Resolve language metadata without translating or guessing shared scripts."""

from __future__ import annotations

import re

from .errors import ContractValidationError

OFFICIAL_STABLE_DIALOGUE_LANGUAGES = (
    "Arabic",
    "Chinese",
    "English",
    "French",
    "German",
    "Italian",
    "Japanese",
    "Korean",
    "Portuguese",
    "Russian",
    "Spanish",
)
RESERVED_NON_LANGUAGE_TAGS = frozenset(
    {"unspecified", "unknown", "auto", "none", "n/a", "language", "lang", "other"}
)
_CANONICAL_NAMES = {name.casefold(): name for name in OFFICIAL_STABLE_DIALOGUE_LANGUAGES}
_LANGUAGE_CODES = dict(
    zip(
        ("ar", "zh", "en", "fr", "de", "it", "ja", "ko", "pt", "ru", "es"),
        OFFICIAL_STABLE_DIALOGUE_LANGUAGES,
        strict=True,
    )
)
_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff]")
_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9d\U0001b000-\U0001b16f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


def normalize_dialogue_language(value: str) -> str:
    """Canonicalize the stable vocabulary; keep valid other names as authored."""

    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ContractValidationError("dialogue language must be a bounded language name")
    folded = value.strip(" ").casefold()
    if folded in RESERVED_NON_LANGUAGE_TAGS:
        raise ContractValidationError("dialogue language must not be a reserved non-language tag")
    if not any(character.isalpha() for character in value) or any(
        not character.isalpha() and character not in " -" for character in value
    ):
        raise ContractValidationError("dialogue language permits only letters, spaces and hyphens")
    return _CANONICAL_NAMES.get(folded, _LANGUAGE_CODES.get(folded, value))


def derive_dialogue_language(text: str) -> str | None:
    """Derive only a uniquely identifiable script; shared scripts remain unknown."""

    hangul = _HANGUL.search(text) is not None
    kana = _KANA.search(text) is not None
    if hangul and kana:
        return None
    if hangul:
        return "Korean"
    if kana:
        return "Japanese"
    if _HAN.search(text) is not None:
        return "Chinese"
    return None
