"""Bounded prompt parsing with explicit profile inputs and visible recovery limits."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import (
    ProfileIdentity,
    PromptProfile,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ProfileRegistryError, PromptParseError
from .profiles import PromptProfileRegistry, default_profile_registry

_MAX_TEXT_LENGTH = 65_536
_MAX_ITEMS = 256
_MAX_DIAGNOSTICS = 512
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_HEADING_PATTERN = re.compile(r"(?m)^(?P<heading>[a-z][a-z0-9_.-]{0,63}):(?P<separator>[ \t]*)")
_I2VA_PREAMBLE = re.compile(
    r"For the target video, at [0-9]+(?:\.[0-9]+)? seconds into the target video, "
    r"<Picture [1-9][0-9]*> \(from \[Shot 1\]\) is fully referenced\."
)
_FRAME_ALIGNMENT_PREAMBLE = re.compile(
    r"How the reference pictures align with the target video — [^\n]+ "
    r"(?:aligns|align) with the [0-9]+(?:\.[0-9]+)?-second mark of the target video\."
)


def _require_source_text(value: object, field: str = "prompt text") -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_LENGTH:
        raise PromptParseError(f"{field} must be a bounded non-empty string")
    if any(
        (ord(character) < 0x20 and character not in "\n\r\t")
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise PromptParseError(f"{field} contains an unsafe control or surrogate code point")
    return value


def _require_fragment(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT_LENGTH:
        raise PromptParseError(f"{field} must be a bounded string")
    if any(
        (ord(character) < 0x20 and character not in "\n\r\t")
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise PromptParseError(f"{field} contains an unsafe control or surrogate code point")
    return value


def _require_offset(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise PromptParseError(f"{field} must be a bounded source offset")
    return value


def _require_code(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise PromptParseError(f"{field} must be a lower-case bounded code")
    return value


@dataclass(frozen=True, slots=True)
class PromptParsedField:
    """One profile-declared field recovered from source text."""

    name: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int

    def __post_init__(self) -> None:
        if _IDENTIFIER_PATTERN.fullmatch(self.name) is None:
            raise PromptParseError("parsed field name must be a lower-case identifier")
        _require_fragment(self.value, "parsed field value")
        if not 0 <= self.start < self.end:
            raise PromptParseError("parsed field span must be non-empty and ordered")
        if not self.start <= self.value_start <= self.value_end <= self.end:
            raise PromptParseError("parsed field value span must lie within its field span")

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "start": self.start,
            "end": self.end,
            "value_start": self.value_start,
            "value_end": self.value_end,
        }


@dataclass(frozen=True, slots=True)
class PromptRecognizedPreamble:
    """A renderer alignment preamble recognized without inferring plan semantics."""

    text: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _require_fragment(self.text, "recognized preamble")
        if not 0 <= self.start < self.end:
            raise PromptParseError("recognized preamble span must be non-empty and ordered")

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text, "start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class PromptUnparsedSpan:
    """Source text that the profile grammar did not claim to understand."""

    text: str
    start: int
    end: int
    reason: str

    def __post_init__(self) -> None:
        _require_fragment(self.text, "unparsed span text")
        if not 0 <= self.start < self.end:
            raise PromptParseError("unparsed span must be non-empty and ordered")
        _require_fragment(self.reason, "unparsed span reason")

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PromptParseDiagnostic:
    """Manual-validation finding that does not copy untrusted source text."""

    severity: ValidationSeverity
    code: str
    message: str
    location: str
    remediation: str

    def __post_init__(self) -> None:
        ValidationDiagnostic(self.severity, self.code, self.message, self.location)
        _require_fragment(self.remediation, "parse diagnostic remediation")

    def to_wire(self) -> dict[str, str]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class PromptParseResult:
    """Inspectable parse output that distinguishes recognized fields from recovery gaps."""

    source_text: str
    profile: ProfileIdentity
    task_mode: TaskMode
    fields: tuple[PromptParsedField, ...]
    preamble: PromptRecognizedPreamble | None
    unparsed_spans: tuple[PromptUnparsedSpan, ...]
    diagnostics: tuple[PromptParseDiagnostic, ...]
    canonical: bool

    def __post_init__(self) -> None:
        source = _require_source_text(self.source_text)
        if not isinstance(self.profile, ProfileIdentity):
            raise PromptParseError("parse profile must be a ProfileIdentity")
        if not isinstance(self.task_mode, TaskMode):
            raise PromptParseError("parse task_mode must be a TaskMode")
        if not isinstance(self.fields, tuple) or len(self.fields) > _MAX_ITEMS:
            raise PromptParseError("parsed fields exceed the bounded result limit")
        if not all(isinstance(value, PromptParsedField) for value in self.fields):
            raise PromptParseError("parsed fields contain an invalid value")
        if self.preamble is not None and not isinstance(self.preamble, PromptRecognizedPreamble):
            raise PromptParseError("parse preamble contains an invalid value")
        if not isinstance(self.unparsed_spans, tuple) or len(self.unparsed_spans) > _MAX_ITEMS:
            raise PromptParseError("unparsed spans exceed the bounded result limit")
        if not all(isinstance(value, PromptUnparsedSpan) for value in self.unparsed_spans):
            raise PromptParseError("unparsed spans contain an invalid value")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > _MAX_DIAGNOSTICS:
            raise PromptParseError("parse diagnostics exceed the bounded result limit")
        if not all(isinstance(value, PromptParseDiagnostic) for value in self.diagnostics):
            raise PromptParseError("parse diagnostics contain an invalid value")
        if not isinstance(self.canonical, bool):
            raise PromptParseError("parse canonical flag must be a bool")
        spans = [(value.start, value.end) for value in self.fields] + [
            (value.start, value.end) for value in self.unparsed_spans
        ]
        if self.preamble is not None:
            spans.append((self.preamble.start, self.preamble.end))
        previous_end = -1
        for start, end in sorted(spans):
            _require_offset(start, "parse span start", len(source))
            _require_offset(end, "parse span end", len(source))
            if start >= end or start < previous_end:
                raise PromptParseError("parse spans must be ordered and non-overlapping")
            previous_end = end

    @property
    def recognized_fields(self) -> tuple[PromptParsedField, ...]:
        """Alias that makes the recovery boundary explicit to manual callers."""

        return self.fields

    @property
    def has_errors(self) -> bool:
        return any(
            value.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for value in self.diagnostics
        )

    @property
    def is_valid(self) -> bool:
        """Return true only for a complete canonical layout with no recovery gaps."""

        return self.canonical and not self.has_errors and not self.unparsed_spans

    def to_wire(self) -> dict[str, object]:
        return {
            "source_text": self.source_text,
            "profile": self.profile.to_wire(),
            "task_mode": self.task_mode.value,
            "fields": [value.to_wire() for value in self.fields],
            "preamble": None if self.preamble is None else self.preamble.to_wire(),
            "unparsed_spans": [value.to_wire() for value in self.unparsed_spans],
            "diagnostics": [value.to_wire() for value in self.diagnostics],
            "canonical": self.canonical,
            "is_valid": self.is_valid,
        }


def _add_diagnostic(
    diagnostics: list[PromptParseDiagnostic],
    severity: ValidationSeverity,
    code: str,
    message: str,
    location: str,
    remediation: str,
) -> None:
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(PromptParseDiagnostic(severity, code, message, location, remediation))


def _is_mode_preamble(task_mode: TaskMode, text: str) -> bool:
    if task_mode is TaskMode.I2VA:
        return _I2VA_PREAMBLE.fullmatch(text) is not None
    if task_mode in {TaskMode.FL2VA, TaskMode.L2VA}:
        return _FRAME_ALIGNMENT_PREAMBLE.fullmatch(text) is not None
    return False


def _span(
    source: str,
    start: int,
    end: int,
    reason: str,
) -> PromptUnparsedSpan | None:
    if start >= end:
        return None
    return PromptUnparsedSpan(source[start:end], start, end, reason)


def _canonical_text(result: PromptParseResult) -> str:
    prefix = ""
    if result.preamble is not None:
        prefix = f"{result.preamble.text}\n\n"
    body = "\n\n".join(f"{field.name}: {field.value}" for field in result.fields)
    return prefix + body


def parse_prompt(
    text: str,
    profile: ProfileIdentity,
    task_mode: TaskMode,
    registry: PromptProfileRegistry | None = None,
) -> PromptParseResult:
    """Parse explicit profile fields without inferring or rewriting prompt content."""

    source = _require_source_text(text)
    if not isinstance(profile, ProfileIdentity):
        raise PromptParseError("prompt parsing requires an explicit ProfileIdentity")
    if not isinstance(task_mode, TaskMode):
        raise PromptParseError("prompt parsing requires an explicit TaskMode")
    profiles = default_profile_registry() if registry is None else registry
    if not isinstance(profiles, PromptProfileRegistry):
        raise PromptParseError("prompt parsing requires a PromptProfileRegistry")
    try:
        definition = profiles.get(profile)
    except ProfileRegistryError as exc:
        raise PromptParseError("prompt profile is not registered") from exc
    if not definition.supports(task_mode):
        raise PromptParseError("prompt profile does not support the explicit task mode")

    expected = definition.render_order
    expected_set = set(expected)
    matches = tuple(_HEADING_PATTERN.finditer(source))
    fields: list[PromptParsedField] = []
    unparsed: list[PromptUnparsedSpan] = []
    preamble: PromptRecognizedPreamble | None = None

    if not matches:
        span = _span(source, 0, len(source), "no profile-declared field heading was recognized")
        if span is not None:
            unparsed.append(span)
    else:
        first_start = matches[0].start()
        prefix = source[:first_start]
        if prefix:
            preamble_text = prefix[:-2] if prefix.endswith("\n\n") else ""
            if preamble_text and _is_mode_preamble(task_mode, preamble_text):
                preamble = PromptRecognizedPreamble(preamble_text, 0, len(preamble_text))
            else:
                span = _span(source, 0, first_start, "text before the first recognized field")
                if span is not None:
                    unparsed.append(span)

        for index, match in enumerate(matches):
            block_end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            heading = match.group("heading")
            separator = match.group("separator")
            valid_heading = bool(separator) and heading in expected_set
            if not valid_heading:
                span = _span(source, match.start(), block_end, "unrecognized or malformed field")
                if span is not None:
                    unparsed.append(span)
                continue
            value_start = match.end()
            value_end = block_end
            value_text = source[value_start:value_end]
            if value_text.endswith("\n\n"):
                value_end -= 2
            elif value_text.endswith("\n"):
                value_end -= 1
            fields.append(
                PromptParsedField(
                    heading,
                    source[value_start:value_end],
                    match.start(),
                    value_end,
                    value_start,
                    value_end,
                )
            )

    headings = tuple(field.name for field in fields)
    diagnostics: list[PromptParseDiagnostic] = []
    for field in expected:
        count = headings.count(field)
        if count == 0:
            _add_diagnostic(
                diagnostics,
                ValidationSeverity.ERROR,
                "parse.missing_field",
                "a required profile field was not recognized",
                f"profile.required_fields.{field}",
                "Add the exact profile-declared heading and a bounded field value.",
            )
        elif count > 1:
            _add_diagnostic(
                diagnostics,
                ValidationSeverity.ERROR,
                "parse.duplicate_field",
                "a profile field heading appears more than once",
                f"prompt.fields.{field}",
                "Keep one occurrence in the declared profile order.",
            )
    if headings != expected:
        _add_diagnostic(
            diagnostics,
            ValidationSeverity.ERROR,
            "parse.field_order",
            "recognized profile fields are not in the declared render order",
            "prompt.fields",
            "Reorder fields to the explicit profile render order without rewriting values.",
        )
    for parsed_field in fields:
        if not parsed_field.value:
            _add_diagnostic(
                diagnostics,
                ValidationSeverity.ERROR,
                "parse.empty_field",
                "a recognized profile field has no value",
                f"prompt.fields.{parsed_field.name}",
                "Provide a non-empty value or keep the field visibly unparsed for manual review.",
            )
    for span in unparsed:
        _add_diagnostic(
            diagnostics,
            ValidationSeverity.WARNING,
            "parse.unparsed_span",
            "source text was retained but not claimed by the profile grammar",
            f"source[{span.start}:{span.end}]",
            "Review the span manually; it cannot be treated as canonical prompt structure.",
        )

    provisional = PromptParseResult(
        source,
        profile,
        task_mode,
        tuple(fields),
        preamble,
        tuple(unparsed),
        tuple(diagnostics),
        False,
    )
    requires_preamble = profile.name is PromptProfile.BASE and task_mode in {
        TaskMode.I2VA,
        TaskMode.FL2VA,
        TaskMode.L2VA,
    }
    preamble_ok = (
        provisional.preamble is not None and _is_mode_preamble(task_mode, provisional.preamble.text)
        if requires_preamble
        else provisional.preamble is None
    )
    canonical = (
        headings == expected
        and all(field.value for field in provisional.fields)
        and not provisional.unparsed_spans
        and preamble_ok
        and _canonical_text(provisional) == source
    )
    return PromptParseResult(
        source,
        profile,
        task_mode,
        tuple(fields),
        preamble,
        tuple(unparsed),
        tuple(diagnostics),
        canonical,
    )


def render_parsed_prompt(result: PromptParseResult) -> str:
    """Return source text only for a canonical parse; never repair manual input."""

    if not isinstance(result, PromptParseResult):
        raise PromptParseError("rendering requires a PromptParseResult")
    if not result.canonical or not result.is_valid:
        raise PromptParseError("only a valid canonical parse can be rendered")
    return result.source_text


__all__ = [
    "PromptParsedField",
    "PromptRecognizedPreamble",
    "PromptUnparsedSpan",
    "PromptParseDiagnostic",
    "PromptParseResult",
    "parse_prompt",
    "render_parsed_prompt",
]
