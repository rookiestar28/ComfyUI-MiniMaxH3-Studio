"""Closed model-owned descriptions and bindings to caller-owned dialogue facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .constraints import ExactTextConstraint, ExactTextKind
from .dialogue_speakers import speaker_entries

MAX_INTENT_DESCRIPTION_CHARS = 4096
MAX_DIALOGUE_BINDINGS = 256
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SPEAKER_KEY = re.compile(r"(?:@anonymous:)?[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SemanticIntentError(ValueError):
    """A content-free failure at the typed proposal boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SemanticIntentKind(str, Enum):
    SUBJECT = "subject"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    AUDIO = "audio"


def _plain_text(value: object, maximum: int, code: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise SemanticIntentError(code)
    if any(
        ord(char) < 32
        or 127 <= ord(char) <= 159
        or ord(char) in {0x2028, 0x2029}
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise SemanticIntentError(code)
    return value


@dataclass(frozen=True, slots=True)
class DialogueSemanticBinding:
    constraint_id: str
    language: str | None
    speaker_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.constraint_id) is not str or _IDENTIFIER.fullmatch(self.constraint_id) is None:
            raise SemanticIntentError("dialogue_constraint_id")
        if self.language is not None:
            _plain_text(self.language, 64, "dialogue_language")
        if (
            type(self.speaker_keys) is not tuple
            or not self.speaker_keys
            or len(self.speaker_keys) > MAX_DIALOGUE_BINDINGS
            or any(
                type(key) is not str or _SPEAKER_KEY.fullmatch(key) is None
                for key in self.speaker_keys
            )
        ):
            raise SemanticIntentError("dialogue_speaker_keys")
        if len(set(self.speaker_keys)) != len(self.speaker_keys):
            raise SemanticIntentError("dialogue_duplicate_speakers")

    def to_wire(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "language": self.language,
            "speaker_keys": list(self.speaker_keys),
        }


@dataclass(frozen=True, slots=True)
class TypedSemanticIntent:
    kind: SemanticIntentKind
    description: str
    dialogue_bindings: tuple[DialogueSemanticBinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not SemanticIntentKind:
            raise SemanticIntentError("intent_kind")
        _plain_text(self.description, MAX_INTENT_DESCRIPTION_CHARS, "intent_description")
        # IMPORTANT: the renderer inserts H3 structure around these descriptions. Allowing model
        # tags or block delimiters here would let untrusted prose author that structure directly.
        if any(marker in self.description for marker in "<>[]"):
            raise SemanticIntentError("intent_renderer_markup")
        if (
            type(self.dialogue_bindings) is not tuple
            or len(self.dialogue_bindings) > MAX_DIALOGUE_BINDINGS
            or any(
                type(binding) is not DialogueSemanticBinding for binding in self.dialogue_bindings
            )
        ):
            raise SemanticIntentError("intent_dialogue_bindings")
        identifiers = tuple(binding.constraint_id for binding in self.dialogue_bindings)
        if len(set(identifiers)) != len(identifiers):
            raise SemanticIntentError("dialogue_duplicate_bindings")
        if self.dialogue_bindings and self.kind is not SemanticIntentKind.AUDIO:
            raise SemanticIntentError("dialogue_requires_audio_intent")

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "dialogue_bindings": [binding.to_wire() for binding in self.dialogue_bindings],
        }


def decode_typed_semantic_intent(value: object) -> TypedSemanticIntent:
    if not isinstance(value, Mapping) or set(value) != {"kind", "description", "dialogue_bindings"}:
        raise SemanticIntentError("intent_shape")
    kind_value = value["kind"]
    if type(kind_value) is not str:
        raise SemanticIntentError("intent_kind")
    try:
        kind = SemanticIntentKind(kind_value)
    except ValueError:
        raise SemanticIntentError("intent_kind") from None
    description = value["description"]
    if type(description) is not str:
        raise SemanticIntentError("intent_description")
    bindings_value = value["dialogue_bindings"]
    if type(bindings_value) is not list or len(bindings_value) > MAX_DIALOGUE_BINDINGS:
        raise SemanticIntentError("intent_dialogue_bindings")
    bindings: list[DialogueSemanticBinding] = []
    for binding in bindings_value:
        if not isinstance(binding, Mapping) or set(binding) != {
            "constraint_id",
            "language",
            "speaker_keys",
        }:
            raise SemanticIntentError("dialogue_binding_shape")
        identifier, language, keys = (
            binding["constraint_id"],
            binding["language"],
            binding["speaker_keys"],
        )
        if type(identifier) is not str or (language is not None and type(language) is not str):
            raise SemanticIntentError("dialogue_binding_shape")
        if type(keys) is not list or not all(type(key) is str for key in keys):
            raise SemanticIntentError("dialogue_speaker_keys")
        bindings.append(DialogueSemanticBinding(identifier, language, tuple(keys)))
    return TypedSemanticIntent(kind, description, tuple(bindings))


def semantic_dialogue_catalog(
    accepted: tuple[ExactTextConstraint, ...],
) -> tuple[DialogueSemanticBinding, ...]:
    approved: dict[str, DialogueSemanticBinding] = {}
    for constraint in accepted:
        if constraint.kind not in {ExactTextKind.DIALOGUE, ExactTextKind.LYRICS}:
            continue
        if constraint.constraint_id in approved:
            raise SemanticIntentError("dialogue_source_ambiguous")
        approved[constraint.constraint_id] = DialogueSemanticBinding(
            constraint.constraint_id,
            constraint.language,
            tuple(key for key, _speaker in speaker_entries(constraint)),
        )
    return tuple(approved.values())


def validate_semantic_dialogue_bindings(
    intent: TypedSemanticIntent, accepted: tuple[ExactTextConstraint, ...]
) -> None:
    approved = {binding.constraint_id: binding for binding in semantic_dialogue_catalog(accepted)}
    for binding in intent.dialogue_bindings:
        # SECURITY: a syntactically valid key is no authority to invent speakers or a language.
        # Compare the entire binding against the caller-owned constraint, including anonymous keys.
        if approved.get(binding.constraint_id) != binding:
            raise SemanticIntentError("dialogue_binding_mutation")
