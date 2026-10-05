"""Immutable user-owned H3 hard constraints.

The values in this module are deliberately boring: they retain what the caller declared and do not
attempt to improve, translate, infer, or render it. Later providers may propose additional detail,
but they cannot replace these values without an explicit transformation record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum

from .dialogue_language import normalize_dialogue_language
from .errors import (
    ConstraintConflictError,
    ConstraintTransformationError,
    ContractValidationError,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DECIMAL_TIME_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_MAX_TEXT_LENGTH = 65_536
_MAX_IDENTIFIER_LENGTH = 128
_MAX_TIME_SOURCE_LENGTH = 64
_MAX_AUTHORIZER_LENGTH = 256
_MAX_REASON_LENGTH = 4096
_MAX_LOCATION_LENGTH = 256
_MAX_TIME_SECONDS = Decimal("86400")


class ExactTextKind(str, Enum):
    """The H3 text classes whose caller-declared spelling is hard."""

    DIALOGUE = "dialogue"
    LYRICS = "lyrics"
    VISIBLE_TEXT = "visible_text"


class ContentScope(str, Enum):
    """The semantic owner of required or forbidden content."""

    GENERAL = "general"
    VISUAL = "visual"
    AUDIO = "audio"
    DIALOGUE = "dialogue"
    LYRICS = "lyrics"
    VISIBLE_TEXT = "visible_text"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    ASSET = "asset"


class DirectiveTarget(str, Enum):
    """The owned target of a keep/change instruction."""

    SUBJECT = "subject"
    SCENE = "scene"
    ACTION = "action"
    CAMERA = "camera"
    STYLE = "style"
    AUDIO = "audio"
    DIALOGUE = "dialogue"
    LYRICS = "lyrics"
    VISIBLE_TEXT = "visible_text"
    ASSET = "asset"


class KeepChangeAction(str, Enum):
    """Whether a declared target must remain or be explicitly changed."""

    KEEP = "keep"
    CHANGE = "change"


class TransformationField(str, Enum):
    """The typed field an authorized transformation is allowed to replace."""

    TEXT = "text"
    START = "start"
    END = "end"
    CONTENT = "content"
    VALUE = "value"
    REPLACEMENT = "replacement"


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field} must be a {expected.__name__}")


def _require_identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_exact_text(value: object, field: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    """Validate without changing the caller's Unicode, whitespace, or punctuation."""

    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field} must be a non-empty bounded string")
    for character in value:
        codepoint = ord(character)
        if codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF:
            raise ContractValidationError(f"{field} contains an unsafe wire code point")
    return value


def _require_optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _require_exact_text(value, field, maximum)


def _decimal_wire(value: Decimal) -> str:
    return format(value, "f")


def _parse_time_source(raw: str) -> Decimal:
    if ":" not in raw:
        if _DECIMAL_TIME_PATTERN.fullmatch(raw) is None:
            raise ContractValidationError(
                "time source must be decimal seconds or MM:SS[.fraction]/HH:MM:SS[.fraction]"
            )
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise ContractValidationError("time source is not a valid decimal") from exc

    parts = raw.split(":")
    if len(parts) not in {2, 3} or any(not part for part in parts):
        raise ContractValidationError("time source has an invalid clock shape")
    if any(_DECIMAL_TIME_PATTERN.fullmatch(part) is None for part in parts):
        raise ContractValidationError("time source has an invalid clock component")
    try:
        numbers = [Decimal(part) for part in parts]
    except InvalidOperation as exc:
        raise ContractValidationError("time source has an invalid clock component") from exc

    seconds = numbers[-1]
    if seconds >= 60:
        raise ContractValidationError("clock seconds must be less than 60")
    minutes = numbers[-2]
    if minutes >= 60:
        raise ContractValidationError("clock minutes must be less than 60")
    if len(numbers) == 2:
        return minutes * 60 + seconds
    return numbers[0] * 3600 + minutes * 60 + seconds


class DialogueDelivery(str, Enum):
    ON_SCREEN = "on_screen"
    VOICEOVER = "voiceover"


@dataclass(frozen=True, slots=True)
class DialogueSpeaker:
    """An authored identity; a stable key does not imply a perceived person."""

    key: str
    subject_id: str | None = None
    identity: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.key, "speaker key")
        if self.subject_id is not None:
            _require_identifier(self.subject_id, "speaker subject_id")
        _require_optional_text(self.identity, "speaker identity", 256)

    def to_wire(self) -> dict[str, str | None]:
        return {"key": self.key, "subject_id": self.subject_id, "identity": self.identity}


@dataclass(frozen=True, slots=True)
class ExactTextConstraint:
    """A dialogue, lyric, or visible-text value owned by the caller."""

    constraint_id: str
    kind: ExactTextKind
    text: str
    language: str | None = None
    location: str | None = None
    speakers: tuple[DialogueSpeaker, ...] = ()
    delivery: DialogueDelivery = DialogueDelivery.ON_SCREEN
    segment_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.constraint_id, "constraint_id")
        _require_enum(self.kind, ExactTextKind, "exact text kind")
        _require_exact_text(self.text, "text")
        _require_optional_text(self.language, "language", 64)
        _require_optional_text(self.location, "location", _MAX_LOCATION_LENGTH)
        if (
            not isinstance(self.speakers, tuple)
            or len(self.speakers) > 256
            or not all(type(value) is DialogueSpeaker for value in self.speakers)
        ):
            raise ContractValidationError("speakers must be a bounded tuple of DialogueSpeaker")
        if len({value.key for value in self.speakers}) != len(self.speakers):
            raise ContractValidationError("duplicate dialogue speaker key")
        _require_enum(self.delivery, DialogueDelivery, "dialogue delivery")
        if self.segment_id is not None:
            _require_identifier(self.segment_id, "dialogue segment_id")
        if self.kind not in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS) and (
            self.speakers or self.delivery is not DialogueDelivery.ON_SCREEN or self.segment_id
        ):
            raise ContractValidationError(
                "speaker, delivery and segment apply only to dialogue/lyrics"
            )
        if (
            self.kind in (ExactTextKind.DIALOGUE, ExactTextKind.LYRICS)
            and self.language is not None
        ):
            object.__setattr__(self, "language", normalize_dialogue_language(self.language))

    def to_wire(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "kind": self.kind.value,
            "text": self.text,
            "language": self.language,
            "location": self.location,
            "speakers": [value.to_wire() for value in self.speakers],
            "delivery": self.delivery.value,
            "segment_id": self.segment_id,
        }


@dataclass(frozen=True, slots=True)
class TimePoint:
    """A bounded time with both typed seconds and its exact source spelling."""

    raw: str
    seconds: Decimal

    def __post_init__(self) -> None:
        _require_exact_text(self.raw, "time raw", _MAX_TIME_SOURCE_LENGTH)
        if not isinstance(self.seconds, Decimal) or not self.seconds.is_finite():
            raise ContractValidationError("time seconds must be a finite Decimal")
        if self.seconds < 0 or self.seconds > _MAX_TIME_SECONDS:
            raise ContractValidationError("time seconds must be between 0 and 86400")
        parsed = _parse_time_source(self.raw)
        if parsed != self.seconds:
            raise ContractValidationError("time raw and typed seconds disagree")

    @classmethod
    def from_text(cls, raw: str) -> TimePoint:
        value = _require_exact_text(raw, "time raw", _MAX_TIME_SOURCE_LENGTH)
        seconds = _parse_time_source(value)
        return cls(raw=value, seconds=seconds)

    def to_wire(self) -> dict[str, str]:
        return {"raw": self.raw, "seconds": _decimal_wire(self.seconds)}


@dataclass(frozen=True, slots=True)
class TimingConstraint:
    """A user-owned instant or range in the target timeline."""

    constraint_id: str
    start: TimePoint
    end: TimePoint | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.constraint_id, "constraint_id")
        if not isinstance(self.start, TimePoint):
            raise ContractValidationError("timing start must be a TimePoint")
        if self.end is not None and not isinstance(self.end, TimePoint):
            raise ContractValidationError("timing end must be a TimePoint or None")
        if self.end is not None and self.end.seconds < self.start.seconds:
            raise ContractValidationError("timing end must not precede timing start")
        _require_optional_text(self.label, "timing label", _MAX_LOCATION_LENGTH)

    def to_wire(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "start": self.start.to_wire(),
            "end": None if self.end is None else self.end.to_wire(),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class RequiredContent:
    """Content that must appear in the owned scope."""

    constraint_id: str
    scope: ContentScope
    content: str
    asset_id: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.constraint_id, "constraint_id")
        _require_enum(self.scope, ContentScope, "required content scope")
        _require_exact_text(self.content, "required content")
        if self.asset_id is not None:
            _require_identifier(self.asset_id, "required content asset_id")
        _require_optional_text(self.location, "required content location", _MAX_LOCATION_LENGTH)

    def to_wire(self) -> dict[str, str | None]:
        return {
            "constraint_id": self.constraint_id,
            "kind": "required",
            "scope": self.scope.value,
            "content": self.content,
            "asset_id": self.asset_id,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class ForbiddenContent:
    """Content that must not appear in the owned scope."""

    constraint_id: str
    scope: ContentScope
    content: str
    asset_id: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.constraint_id, "constraint_id")
        _require_enum(self.scope, ContentScope, "forbidden content scope")
        _require_exact_text(self.content, "forbidden content")
        if self.asset_id is not None:
            _require_identifier(self.asset_id, "forbidden content asset_id")
        _require_optional_text(self.location, "forbidden content location", _MAX_LOCATION_LENGTH)

    def to_wire(self) -> dict[str, str | None]:
        return {
            "constraint_id": self.constraint_id,
            "kind": "forbidden",
            "scope": self.scope.value,
            "content": self.content,
            "asset_id": self.asset_id,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class KeepChangeDirective:
    """An explicit user instruction to preserve or replace an owned target."""

    constraint_id: str
    action: KeepChangeAction
    target: DirectiveTarget
    value: str
    replacement: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.constraint_id, "constraint_id")
        _require_enum(self.action, KeepChangeAction, "keep/change action")
        _require_enum(self.target, DirectiveTarget, "directive target")
        _require_exact_text(self.value, "directive value")
        if self.action is KeepChangeAction.KEEP and self.replacement is not None:
            raise ContractValidationError("keep directives cannot contain a replacement")
        if self.action is KeepChangeAction.CHANGE:
            _require_exact_text(self.replacement, "directive replacement")

    def to_wire(self) -> dict[str, str | None]:
        return {
            "constraint_id": self.constraint_id,
            "action": self.action.value,
            "target": self.target.value,
            "value": self.value,
            "replacement": self.replacement,
        }


@dataclass(frozen=True, slots=True)
class ConstraintTransformation:
    """An explicit authorization/audit record for replacing one declared field."""

    transformation_id: str
    constraint_id: str
    field: TransformationField
    original_value: str
    transformed_value: str
    authorized_by: str
    reason: str

    def __post_init__(self) -> None:
        _require_identifier(self.transformation_id, "transformation_id")
        _require_identifier(self.constraint_id, "constraint_id")
        _require_enum(self.field, TransformationField, "transformation field")
        _require_exact_text(self.original_value, "transformation original_value")
        _require_exact_text(self.transformed_value, "transformation transformed_value")
        if self.original_value == self.transformed_value:
            raise ContractValidationError("a transformation must change the declared value")
        _require_exact_text(
            self.authorized_by, "transformation authorized_by", _MAX_AUTHORIZER_LENGTH
        )
        _require_exact_text(self.reason, "transformation reason", _MAX_REASON_LENGTH)

    def to_wire(self) -> dict[str, str]:
        return {
            "transformation_id": self.transformation_id,
            "constraint_id": self.constraint_id,
            "field": self.field.value,
            "original_value": self.original_value,
            "transformed_value": self.transformed_value,
            "authorized_by": self.authorized_by,
            "reason": self.reason,
        }


ConstraintValue = (
    ExactTextConstraint
    | TimingConstraint
    | RequiredContent
    | ForbiddenContent
    | KeepChangeDirective
)
_CONSTRAINT_TYPES = (
    ExactTextConstraint,
    TimingConstraint,
    RequiredContent,
    ForbiddenContent,
    KeepChangeDirective,
)


def _constraint_id(value: ConstraintValue) -> str:
    return value.constraint_id


@dataclass(frozen=True, slots=True)
class HardConstraintSet:
    """Immutable ordered constraints plus explicit transformation history."""

    constraints: tuple[ConstraintValue, ...] = ()
    transformations: tuple[ConstraintTransformation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.constraints, tuple) or not all(
            isinstance(value, _CONSTRAINT_TYPES) for value in self.constraints
        ):
            raise ContractValidationError("constraints must be a tuple of typed constraint values")
        seen_ids: set[str] = set()
        speakers: dict[str, DialogueSpeaker] = {}
        for value in self.constraints:
            identifier = _constraint_id(value)
            if identifier in seen_ids:
                raise ContractValidationError(f"duplicate hard constraint id: {identifier}")
            seen_ids.add(identifier)
            if isinstance(value, ExactTextConstraint):
                for speaker in value.speakers:
                    if speaker.key in speakers and speakers[speaker.key] != speaker:
                        raise ContractValidationError("speaker_identity_conflict")
                    speakers[speaker.key] = speaker
        if not isinstance(self.transformations, tuple) or not all(
            isinstance(value, ConstraintTransformation) for value in self.transformations
        ):
            raise ContractValidationError(
                "transformations must be a tuple of ConstraintTransformation values"
            )
        seen_transformations: set[str] = set()
        transformation_state: dict[tuple[str, TransformationField], str] = {}
        for transformation in self.transformations:
            if transformation.transformation_id in seen_transformations:
                raise ContractValidationError(
                    f"duplicate transformation id: {transformation.transformation_id}"
                )
            if transformation.constraint_id not in seen_ids:
                raise ContractValidationError(
                    "transformation must target an existing hard constraint"
                )
            field_key = (transformation.constraint_id, transformation.field)
            previous = transformation_state.get(field_key)
            if previous is None:
                previous = transformation.original_value
            if previous != transformation.original_value:
                raise ConstraintTransformationError(
                    "transformation history does not form a value-preserving chain"
                )
            transformation_state[field_key] = transformation.transformed_value
            seen_transformations.add(transformation.transformation_id)

        for (constraint_id, field), expected in transformation_state.items():
            target = next(
                value for value in self.constraints if value.constraint_id == constraint_id
            )
            if self._field_value(target, field) != expected:
                raise ConstraintTransformationError(
                    "transformation history does not match the current constraint value"
                )

    @classmethod
    def empty(cls) -> HardConstraintSet:
        return cls()

    @property
    def exact_texts(self) -> tuple[ExactTextConstraint, ...]:
        return tuple(value for value in self.constraints if isinstance(value, ExactTextConstraint))

    @property
    def timings(self) -> tuple[TimingConstraint, ...]:
        return tuple(value for value in self.constraints if isinstance(value, TimingConstraint))

    @property
    def required_content(self) -> tuple[RequiredContent, ...]:
        return tuple(value for value in self.constraints if isinstance(value, RequiredContent))

    @property
    def forbidden_content(self) -> tuple[ForbiddenContent, ...]:
        return tuple(value for value in self.constraints if isinstance(value, ForbiddenContent))

    @property
    def keep_change_directives(self) -> tuple[KeepChangeDirective, ...]:
        return tuple(value for value in self.constraints if isinstance(value, KeepChangeDirective))

    def to_wire(self) -> dict[str, object]:
        return {
            "constraints": [value.to_wire() for value in self.constraints],
            "transformations": [value.to_wire() for value in self.transformations],
        }

    def apply_authorized_transformation(
        self, transformation: ConstraintTransformation
    ) -> HardConstraintSet:
        """Apply one matching transformation and append its immutable audit record."""

        if not isinstance(transformation, ConstraintTransformation):
            raise ConstraintTransformationError("transformation must be a ConstraintTransformation")
        if any(
            value.transformation_id == transformation.transformation_id
            for value in self.transformations
        ):
            raise ConstraintTransformationError("transformation_id has already been recorded")

        index = next(
            (
                position
                for position, value in enumerate(self.constraints)
                if value.constraint_id == transformation.constraint_id
            ),
            None,
        )
        if index is None:
            raise ConstraintTransformationError("transformation targets an unknown hard constraint")
        target = self.constraints[index]
        current_value = self._field_value(target, transformation.field)
        if current_value != transformation.original_value:
            raise ConstraintTransformationError(
                "transformation original_value does not match the current declared value"
            )
        updated = self._replace_field(target, transformation)
        values = list(self.constraints)
        values[index] = updated
        return HardConstraintSet(tuple(values), self.transformations + (transformation,))

    @staticmethod
    def _field_value(value: ConstraintValue, field: TransformationField) -> str:
        if isinstance(value, ExactTextConstraint) and field is TransformationField.TEXT:
            return value.text
        if isinstance(value, TimingConstraint):
            if field is TransformationField.START:
                return value.start.raw
            if field is TransformationField.END and value.end is not None:
                return value.end.raw
        if (
            isinstance(value, (RequiredContent, ForbiddenContent))
            and field is TransformationField.CONTENT
        ):
            return value.content
        if isinstance(value, KeepChangeDirective):
            if field is TransformationField.VALUE:
                return value.value
            if field is TransformationField.REPLACEMENT and value.replacement is not None:
                return value.replacement
        raise ConstraintTransformationError(
            f"transformation field {field.value} is not valid for {type(value).__name__}"
        )

    @staticmethod
    def _replace_field(
        value: ConstraintValue, transformation: ConstraintTransformation
    ) -> ConstraintValue:
        field = transformation.field
        replacement = transformation.transformed_value
        if isinstance(value, ExactTextConstraint) and field is TransformationField.TEXT:
            return replace(value, text=replacement)
        if isinstance(value, TimingConstraint):
            if field is TransformationField.START:
                return replace(value, start=TimePoint.from_text(replacement))
            if field is TransformationField.END:
                if value.end is None:
                    raise ConstraintTransformationError("cannot transform a missing timing end")
                return replace(value, end=TimePoint.from_text(replacement))
        if isinstance(value, RequiredContent) and field is TransformationField.CONTENT:
            return replace(value, content=replacement)
        if isinstance(value, ForbiddenContent) and field is TransformationField.CONTENT:
            return replace(value, content=replacement)
        if isinstance(value, KeepChangeDirective):
            if field is TransformationField.VALUE:
                return replace(value, value=replacement)
            if field is TransformationField.REPLACEMENT:
                return replace(value, replacement=replacement)
        raise ConstraintTransformationError(
            f"transformation field {field.value} is not valid for {type(value).__name__}"
        )


def normalize_constraints(
    values: Iterable[ConstraintValue] | HardConstraintSet,
) -> HardConstraintSet:
    """Copy a caller iterable into an immutable set without rewriting any values."""

    if isinstance(values, HardConstraintSet):
        return values
    try:
        copied = tuple(values)
    except TypeError as exc:
        raise ContractValidationError("constraints must be an iterable of typed values") from exc
    return HardConstraintSet(copied)


def merge_constraints(*sets: HardConstraintSet) -> HardConstraintSet:
    """Merge sets in argument/insertion order and fail closed on same-ID conflicts."""

    constraints: list[ConstraintValue] = []
    by_id: dict[str, ConstraintValue] = {}
    transformations: list[ConstraintTransformation] = []
    transformations_by_id: dict[str, ConstraintTransformation] = {}
    for constraint_set in sets:
        if not isinstance(constraint_set, HardConstraintSet):
            raise ContractValidationError("merge inputs must be HardConstraintSet values")
        for value in constraint_set.constraints:
            existing = by_id.get(value.constraint_id)
            if existing is None:
                by_id[value.constraint_id] = value
                constraints.append(value)
            elif existing != value:
                raise ConstraintConflictError(
                    f"hard constraint id {value.constraint_id} has conflicting values"
                )
        for transformation in constraint_set.transformations:
            existing_transformation = transformations_by_id.get(transformation.transformation_id)
            if existing_transformation is None:
                transformations_by_id[transformation.transformation_id] = transformation
                transformations.append(transformation)
            elif existing_transformation != transformation:
                raise ConstraintConflictError(
                    f"transformation id {transformation.transformation_id} has conflicting values"
                )
    return HardConstraintSet(tuple(constraints), tuple(transformations))


__all__ = [
    "DialogueDelivery",
    "DialogueSpeaker",
    "ConstraintConflictError",
    "ConstraintTransformationError",
    "ConstraintTransformation",
    "ContentScope",
    "DirectiveTarget",
    "ExactTextConstraint",
    "ExactTextKind",
    "ForbiddenContent",
    "HardConstraintSet",
    "KeepChangeAction",
    "KeepChangeDirective",
    "RequiredContent",
    "TimePoint",
    "TimingConstraint",
    "TransformationField",
    "merge_constraints",
    "normalize_constraints",
]
