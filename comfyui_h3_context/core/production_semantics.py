"""Bounded semantic authority for segmented Production prompts.

The contract carries source-owned semantic content through slicing.  It does not infer timing from
arbitrary prose, choose cuts, or execute provider work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import cast

from .canonical import canonical_fingerprint
from .constraints import ExactTextConstraint, ForbiddenContent
from .context_reporting import ContextPlan, PromptDocument
from .contracts import AssetRole, MediaKind, TaskMode
from .production_duration import MAX_REQUIRED_CUTS

PRODUCTION_SEMANTIC_AUTHORITY_SCHEMA = "h3.context.production_semantic_authority.v1"
PRODUCTION_SEMANTIC_FIELD_SCHEMA = "h3.context.production_semantic_field.v1"
PRODUCTION_SUBJECT_DEFINITION_SCHEMA = "h3.context.production_subject_definition.v1"
PRODUCTION_REFERENCE_DEFINITION_SCHEMA = "h3.context.production_reference_definition.v1"
PRODUCTION_TIMED_SEMANTIC_SPAN_SCHEMA = "h3.context.production_timed_semantic_span.v1"
PRODUCTION_SEMANTIC_SLICE_SCHEMA = "h3.context.production_semantic_slice.v1"
PRODUCTION_SEMANTIC_CENSUS_SCHEMA = "h3.context.production_semantic_census.v1"
PRODUCTION_SEMANTIC_CENSUS_ENTRY_SCHEMA = "h3.context.production_semantic_census_entry.v1"

MAX_SEMANTIC_ITEMS = 64
MAX_SEMANTIC_TEXT_LENGTH = 65_536
MAX_SEMANTIC_TOTAL_TEXT_LENGTH = 65_536
MAX_PRODUCTION_MILLISECONDS = 86_400_000

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_CANONICAL_BLOCK = re.compile(r"(?P<heading>[a-z][a-z0-9_]{0,63}): (?P<body>[^\x00]+)\Z", re.DOTALL)
_CANONICAL_HEADINGS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "integrated_multimodal_description",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_STORYBOARD_HEADINGS = frozenset({"integrated_multimodal_description", "detailed_description"})
_REFERENCE_TOKEN = re.compile(r"<(?:Picture|Video|Audio) [^<>]{1,64}>")
_SUBJECT_TOKEN = re.compile(r"<Subject [^<>]{1,64}>")
_SHOT_MARKER = re.compile(
    r"(?<!from )\[Shot (?P<ordinal>[1-9][0-9]{0,2})\]"
    r"(?: At (?P<minute>[0-9]{2}):(?P<second>[0-5][0-9])\.(?P<millisecond>[0-9]{3}),)?"
)


class ProductionSemanticError(ValueError):
    """A semantic contract is malformed, contradictory, or cannot be preserved."""


class SemanticSplitPolicyV1(str, Enum):
    CLIP = "clip"
    IMMUTABLE = "immutable"


class SemanticDispositionV1(str, Enum):
    GLOBAL_CARRIED = "global_carried"
    ASSIGNED_REBASED = "assigned_rebased"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


def _closed(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != keys:
        raise ProductionSemanticError(field)
    return cast(dict[str, object], value)


def _id(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ProductionSemanticError(field)
    return value


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value) > MAX_SEMANTIC_TEXT_LENGTH
    ):
        raise ProductionSemanticError(field)
    return value


def _ids(value: object, field: str) -> tuple[str, ...]:
    if (
        type(value) not in {tuple, list}
        or len(cast(tuple[object, ...], value)) > MAX_SEMANTIC_ITEMS
    ):
        raise ProductionSemanticError(field)
    result = tuple(_id(item, field) for item in cast(tuple[object, ...], value))
    if len(result) != len(set(result)):
        raise ProductionSemanticError(field)
    return result


def _millisecond(value: object, field: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= MAX_PRODUCTION_MILLISECONDS:
        raise ProductionSemanticError(field)
    return value


def _bounded_rows(
    value: object, field: str, maximum: int = MAX_SEMANTIC_ITEMS
) -> tuple[object, ...]:
    if type(value) not in {tuple, list} or len(cast(tuple[object, ...], value)) > maximum:
        raise ProductionSemanticError(field)
    return tuple(cast(tuple[object, ...], value))


@dataclass(frozen=True, slots=True)
class SemanticFieldV1:
    field_id: str
    heading: str
    text: str
    subject_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _id(self.field_id, "semantic_field_id")
        if self.heading not in {*_CANONICAL_HEADINGS, "alignment_instruction"}:
            raise ProductionSemanticError("semantic_field_heading")
        _text(self.text, "semantic_field_text")
        _ids(self.subject_ids, "semantic_field_subject_ids")
        _ids(self.asset_ids, "semantic_field_asset_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SEMANTIC_FIELD_SCHEMA,
            "field_id": self.field_id,
            "heading": self.heading,
            "text": self.text,
            "subject_ids": list(self.subject_ids),
            "asset_ids": list(self.asset_ids),
        }

    @classmethod
    def from_wire(cls, value: object) -> SemanticFieldV1:
        wire = _closed(
            value,
            "semantic_field_wire",
            frozenset({"schema", "field_id", "heading", "text", "subject_ids", "asset_ids"}),
        )
        if wire["schema"] != PRODUCTION_SEMANTIC_FIELD_SCHEMA:
            raise ProductionSemanticError("semantic_field_schema")
        return cls(
            field_id=cast(str, wire["field_id"]),
            heading=cast(str, wire["heading"]),
            text=cast(str, wire["text"]),
            subject_ids=_ids(wire["subject_ids"], "semantic_field_subject_ids"),
            asset_ids=_ids(wire["asset_ids"], "semantic_field_asset_ids"),
        )


@dataclass(frozen=True, slots=True)
class SubjectDefinitionV1:
    subject_id: str
    label: str
    connection_order: int
    description: str | None = None
    source_asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _id(self.subject_id, "semantic_subject_id")
        _text(self.label, "semantic_subject_label")
        if (
            type(self.connection_order) is not int
            or not 1 <= self.connection_order <= MAX_SEMANTIC_ITEMS
        ):
            raise ProductionSemanticError("semantic_subject_connection_order")
        if self.description is not None:
            _text(self.description, "semantic_subject_description")
        _ids(self.source_asset_ids, "semantic_subject_source_asset_ids")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SUBJECT_DEFINITION_SCHEMA,
            "subject_id": self.subject_id,
            "label": self.label,
            "connection_order": self.connection_order,
            "description": self.description,
            "source_asset_ids": list(self.source_asset_ids),
        }

    @classmethod
    def from_wire(cls, value: object) -> SubjectDefinitionV1:
        wire = _closed(
            value,
            "semantic_subject_wire",
            frozenset(
                {
                    "schema",
                    "subject_id",
                    "label",
                    "connection_order",
                    "description",
                    "source_asset_ids",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_SUBJECT_DEFINITION_SCHEMA:
            raise ProductionSemanticError("semantic_subject_schema")
        description = wire["description"]
        if description is not None and type(description) is not str:
            raise ProductionSemanticError("semantic_subject_description")
        return cls(
            subject_id=cast(str, wire["subject_id"]),
            label=cast(str, wire["label"]),
            connection_order=cast(int, wire["connection_order"]),
            description=description,
            source_asset_ids=_ids(wire["source_asset_ids"], "semantic_subject_source_asset_ids"),
        )


@dataclass(frozen=True, slots=True)
class ReferenceDefinitionV1:
    asset_id: str
    kind: MediaKind
    role: AssetRole
    connection_order: int
    backend_label: str

    def __post_init__(self) -> None:
        _id(self.asset_id, "semantic_reference_asset_id")
        if type(self.kind) is not MediaKind:
            raise ProductionSemanticError("semantic_reference_kind")
        if type(self.role) is not AssetRole:
            raise ProductionSemanticError("semantic_reference_role")
        if (
            type(self.connection_order) is not int
            or not 1 <= self.connection_order <= MAX_SEMANTIC_ITEMS
        ):
            raise ProductionSemanticError("semantic_reference_connection_order")
        _text(self.backend_label, "semantic_reference_backend_label")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_REFERENCE_DEFINITION_SCHEMA,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "connection_order": self.connection_order,
            "backend_label": self.backend_label,
        }

    @classmethod
    def from_wire(cls, value: object) -> ReferenceDefinitionV1:
        wire = _closed(
            value,
            "semantic_reference_wire",
            frozenset({"schema", "asset_id", "kind", "role", "connection_order", "backend_label"}),
        )
        if wire["schema"] != PRODUCTION_REFERENCE_DEFINITION_SCHEMA:
            raise ProductionSemanticError("semantic_reference_schema")
        try:
            kind = MediaKind(wire["kind"])
            role = AssetRole(wire["role"])
        except (TypeError, ValueError) as exc:
            raise ProductionSemanticError("semantic_reference_enum") from exc
        return cls(
            asset_id=cast(str, wire["asset_id"]),
            kind=kind,
            role=role,
            connection_order=cast(int, wire["connection_order"]),
            backend_label=cast(str, wire["backend_label"]),
        )


@dataclass(frozen=True, slots=True)
class TimedSemanticSpanV1:
    span_id: str
    kind: str
    start_milliseconds: int
    end_milliseconds: int
    text: str
    subject_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    split_policy: SemanticSplitPolicyV1 = SemanticSplitPolicyV1.CLIP
    source_span_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.span_id, "timed_semantic_span_id")
        _id(self.kind, "timed_semantic_kind")
        start = _millisecond(self.start_milliseconds, "timed_semantic_start")
        end = _millisecond(self.end_milliseconds, "timed_semantic_end", allow_zero=False)
        if end <= start:
            raise ProductionSemanticError("timed_semantic_interval")
        _text(self.text, "timed_semantic_text")
        _ids(self.subject_ids, "timed_semantic_subject_ids")
        _ids(self.asset_ids, "timed_semantic_asset_ids")
        if type(self.split_policy) is not SemanticSplitPolicyV1:
            raise ProductionSemanticError("timed_semantic_split_policy")
        if self.source_span_id is not None:
            _id(self.source_span_id, "timed_semantic_source_span_id")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_TIMED_SEMANTIC_SPAN_SCHEMA,
            "span_id": self.span_id,
            "kind": self.kind,
            "start_milliseconds": self.start_milliseconds,
            "end_milliseconds": self.end_milliseconds,
            "text": self.text,
            "subject_ids": list(self.subject_ids),
            "asset_ids": list(self.asset_ids),
            "split_policy": self.split_policy.value,
            "source_span_id": self.source_span_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> TimedSemanticSpanV1:
        wire = _closed(
            value,
            "timed_semantic_wire",
            frozenset(
                {
                    "schema",
                    "span_id",
                    "kind",
                    "start_milliseconds",
                    "end_milliseconds",
                    "text",
                    "subject_ids",
                    "asset_ids",
                    "split_policy",
                    "source_span_id",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_TIMED_SEMANTIC_SPAN_SCHEMA:
            raise ProductionSemanticError("timed_semantic_schema")
        try:
            split_policy = SemanticSplitPolicyV1(wire["split_policy"])
        except (TypeError, ValueError) as exc:
            raise ProductionSemanticError("timed_semantic_split_policy") from exc
        source_span_id = wire["source_span_id"]
        if source_span_id is not None and type(source_span_id) is not str:
            raise ProductionSemanticError("timed_semantic_source_span_id")
        return cls(
            span_id=cast(str, wire["span_id"]),
            kind=cast(str, wire["kind"]),
            start_milliseconds=cast(int, wire["start_milliseconds"]),
            end_milliseconds=cast(int, wire["end_milliseconds"]),
            text=cast(str, wire["text"]),
            subject_ids=_ids(wire["subject_ids"], "timed_semantic_subject_ids"),
            asset_ids=_ids(wire["asset_ids"], "timed_semantic_asset_ids"),
            split_policy=split_policy,
            source_span_id=source_span_id,
        )


@dataclass(frozen=True, slots=True)
class ProductionSemanticAuthorityV1:
    subject_definitions: tuple[SubjectDefinitionV1, ...] = ()
    reference_definitions: tuple[ReferenceDefinitionV1, ...] = ()
    global_fields: tuple[SemanticFieldV1, ...] = ()
    timed_spans: tuple[TimedSemanticSpanV1, ...] = ()
    required_cut_milliseconds: tuple[int, ...] = ()
    forbidden_cut_intervals_milliseconds: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        for values, expected, field in (
            (self.subject_definitions, SubjectDefinitionV1, "semantic_subject_definitions"),
            (self.reference_definitions, ReferenceDefinitionV1, "semantic_reference_definitions"),
            (self.global_fields, SemanticFieldV1, "semantic_global_fields"),
            (self.timed_spans, TimedSemanticSpanV1, "semantic_timed_spans"),
        ):
            if (
                type(values) is not tuple
                or len(values) > MAX_SEMANTIC_ITEMS
                or not all(type(item) is expected for item in values)
            ):
                raise ProductionSemanticError(field)
        for identifiers, identifier_field in (
            ((item.subject_id for item in self.subject_definitions), "semantic_subject_ids"),
            ((item.asset_id for item in self.reference_definitions), "semantic_reference_ids"),
            ((item.field_id for item in self.global_fields), "semantic_field_ids"),
            ((item.span_id for item in self.timed_spans), "semantic_span_ids"),
        ):
            identifier_values = tuple(identifiers)
            if len(identifier_values) != len(set(identifier_values)):
                raise ProductionSemanticError(identifier_field)
        orders = tuple(item.connection_order for item in self.reference_definitions)
        if orders and orders != tuple(range(1, len(orders) + 1)):
            raise ProductionSemanticError("semantic_reference_order")
        subject_orders = tuple(item.connection_order for item in self.subject_definitions)
        if subject_orders and subject_orders != tuple(range(1, len(subject_orders) + 1)):
            raise ProductionSemanticError("semantic_subject_order")
        if (
            type(self.required_cut_milliseconds) is not tuple
            or len(self.required_cut_milliseconds) > MAX_REQUIRED_CUTS
        ):
            raise ProductionSemanticError("semantic_required_cuts")
        cuts = tuple(
            _millisecond(value, "semantic_required_cut", allow_zero=False)
            for value in self.required_cut_milliseconds
        )
        if cuts != tuple(sorted(set(cuts))):
            raise ProductionSemanticError("semantic_required_cuts")
        intervals = self.forbidden_cut_intervals_milliseconds
        if type(intervals) is not tuple or len(intervals) > MAX_SEMANTIC_ITEMS:
            raise ProductionSemanticError("semantic_forbidden_cut_intervals")
        checked_intervals: list[tuple[int, int]] = []
        for interval in intervals:
            if type(interval) is not tuple or len(interval) != 2:
                raise ProductionSemanticError("semantic_forbidden_cut_intervals")
            start = _millisecond(interval[0], "semantic_forbidden_cut_interval")
            end = _millisecond(interval[1], "semantic_forbidden_cut_interval", allow_zero=False)
            if start >= end:
                raise ProductionSemanticError("semantic_forbidden_cut_intervals")
            checked_intervals.append((start, end))
        if intervals != tuple(sorted(set(checked_intervals))):
            raise ProductionSemanticError("semantic_forbidden_cut_intervals")
        headings = tuple(item.heading for item in self.global_fields)
        if len(headings) != len(set(headings)):
            raise ProductionSemanticError("semantic_global_headings")
        label_ordinals = {MediaKind.IMAGE: 0, MediaKind.VIDEO: 0, MediaKind.AUDIO: 0}
        label_names = {
            MediaKind.IMAGE: "Picture",
            MediaKind.VIDEO: "Video",
            MediaKind.AUDIO: "Audio",
        }
        for reference in self.reference_definitions:
            label_ordinals[reference.kind] += 1
            expected_label = f"<{label_names[reference.kind]} {label_ordinals[reference.kind]}>"
            if reference.backend_label != expected_label:
                raise ProductionSemanticError("semantic_reference_backend_label")
        total = sum(len(item.text) for item in self.global_fields) + sum(
            len(item.text) for item in self.timed_spans
        )
        total += sum(
            len(item.label) + len(item.description or "") for item in self.subject_definitions
        )
        total += sum(len(item.backend_label) for item in self.reference_definitions)
        if total > MAX_SEMANTIC_TOTAL_TEXT_LENGTH:
            raise ProductionSemanticError("semantic_total_text")
        validate_semantic_references(self)

    @classmethod
    def empty(cls) -> ProductionSemanticAuthorityV1:
        return cls()

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SEMANTIC_AUTHORITY_SCHEMA,
            "subject_definitions": [item.to_wire() for item in self.subject_definitions],
            "reference_definitions": [item.to_wire() for item in self.reference_definitions],
            "global_fields": [item.to_wire() for item in self.global_fields],
            "timed_spans": [item.to_wire() for item in self.timed_spans],
            "required_cut_milliseconds": list(self.required_cut_milliseconds),
            "forbidden_cut_intervals_milliseconds": [
                list(interval) for interval in self.forbidden_cut_intervals_milliseconds
            ],
        }

    @classmethod
    def from_wire(cls, value: object) -> ProductionSemanticAuthorityV1:
        wire = _closed(
            value,
            "semantic_authority_wire",
            frozenset(
                {
                    "schema",
                    "subject_definitions",
                    "reference_definitions",
                    "global_fields",
                    "timed_spans",
                    "required_cut_milliseconds",
                    "forbidden_cut_intervals_milliseconds",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_SEMANTIC_AUTHORITY_SCHEMA:
            raise ProductionSemanticError("semantic_authority_schema")
        cuts = _bounded_rows(wire["required_cut_milliseconds"], "semantic_required_cuts")
        intervals = _bounded_rows(
            wire["forbidden_cut_intervals_milliseconds"],
            "semantic_forbidden_cut_intervals",
        )
        return cls(
            subject_definitions=tuple(
                SubjectDefinitionV1.from_wire(item)
                for item in _bounded_rows(
                    wire["subject_definitions"], "semantic_subject_definitions"
                )
            ),
            reference_definitions=tuple(
                ReferenceDefinitionV1.from_wire(item)
                for item in _bounded_rows(
                    wire["reference_definitions"], "semantic_reference_definitions"
                )
            ),
            global_fields=tuple(
                SemanticFieldV1.from_wire(item)
                for item in _bounded_rows(wire["global_fields"], "semantic_global_fields")
            ),
            timed_spans=tuple(
                TimedSemanticSpanV1.from_wire(item)
                for item in _bounded_rows(wire["timed_spans"], "semantic_timed_spans")
            ),
            required_cut_milliseconds=tuple(cast(int, item) for item in cuts),
            forbidden_cut_intervals_milliseconds=tuple(
                cast(tuple[int, int], tuple(cast(list[int], item))) for item in intervals
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticCensusEntryV1:
    source_id: str
    disposition: SemanticDispositionV1
    segment_id: str | None = None
    local_start_milliseconds: int | None = None
    local_end_milliseconds: int | None = None

    def __post_init__(self) -> None:
        _id(self.source_id, "semantic_census_source_id")
        if type(self.disposition) is not SemanticDispositionV1:
            raise ProductionSemanticError("semantic_census_disposition")
        if self.segment_id is not None:
            _id(self.segment_id, "semantic_census_segment_id")
        local_values = (self.local_start_milliseconds, self.local_end_milliseconds)
        if any(value is None for value in local_values) != all(
            value is None for value in local_values
        ):
            raise ProductionSemanticError("semantic_census_local_span")
        if self.local_start_milliseconds is not None and self.local_end_milliseconds is not None:
            start = _millisecond(self.local_start_milliseconds, "semantic_census_local_start")
            end = _millisecond(
                self.local_end_milliseconds, "semantic_census_local_end", allow_zero=False
            )
            if end <= start:
                raise ProductionSemanticError("semantic_census_local_span")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SEMANTIC_CENSUS_ENTRY_SCHEMA,
            "source_id": self.source_id,
            "disposition": self.disposition.value,
            "segment_id": self.segment_id,
            "local_start_milliseconds": self.local_start_milliseconds,
            "local_end_milliseconds": self.local_end_milliseconds,
        }

    @classmethod
    def from_wire(cls, value: object) -> SemanticCensusEntryV1:
        wire = _closed(
            value,
            "semantic_census_entry_wire",
            frozenset(
                {
                    "schema",
                    "source_id",
                    "disposition",
                    "segment_id",
                    "local_start_milliseconds",
                    "local_end_milliseconds",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_SEMANTIC_CENSUS_ENTRY_SCHEMA:
            raise ProductionSemanticError("semantic_census_entry_schema")
        try:
            disposition = SemanticDispositionV1(wire["disposition"])
        except (TypeError, ValueError) as exc:
            raise ProductionSemanticError("semantic_census_disposition") from exc
        return cls(
            source_id=cast(str, wire["source_id"]),
            disposition=disposition,
            segment_id=cast(str | None, wire["segment_id"]),
            local_start_milliseconds=cast(int | None, wire["local_start_milliseconds"]),
            local_end_milliseconds=cast(int | None, wire["local_end_milliseconds"]),
        )


@dataclass(frozen=True, slots=True)
class ProductionSemanticSliceV1:
    segment_id: str
    global_start_milliseconds: int
    global_end_milliseconds: int
    task_mode: TaskMode
    asset_ids: tuple[str, ...]
    subject_definitions: tuple[SubjectDefinitionV1, ...]
    reference_definitions: tuple[ReferenceDefinitionV1, ...]
    global_fields: tuple[SemanticFieldV1, ...]
    timed_spans: tuple[TimedSemanticSpanV1, ...]
    census: tuple[SemanticCensusEntryV1, ...]

    def __post_init__(self) -> None:
        _id(self.segment_id, "semantic_slice_segment_id")
        start = _millisecond(self.global_start_milliseconds, "semantic_slice_start")
        end = _millisecond(self.global_end_milliseconds, "semantic_slice_end", allow_zero=False)
        if end <= start:
            raise ProductionSemanticError("semantic_slice_interval")
        if type(self.task_mode) is not TaskMode:
            raise ProductionSemanticError("semantic_slice_task_mode")
        _ids(self.asset_ids, "semantic_slice_asset_ids")
        for values, expected, field in (
            (self.subject_definitions, SubjectDefinitionV1, "semantic_slice_subjects"),
            (self.reference_definitions, ReferenceDefinitionV1, "semantic_slice_references"),
            (self.global_fields, SemanticFieldV1, "semantic_slice_globals"),
            (self.timed_spans, TimedSemanticSpanV1, "semantic_slice_timed_spans"),
            (self.census, SemanticCensusEntryV1, "semantic_slice_census"),
        ):
            maximum = (
                MAX_SEMANTIC_ITEMS * 4 if field == "semantic_slice_census" else MAX_SEMANTIC_ITEMS
            )
            if (
                type(values) is not tuple
                or len(values) > maximum
                or not all(type(item) is expected for item in values)
            ):
                raise ProductionSemanticError(field)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SEMANTIC_SLICE_SCHEMA,
            "segment_id": self.segment_id,
            "global_start_milliseconds": self.global_start_milliseconds,
            "global_end_milliseconds": self.global_end_milliseconds,
            "task_mode": self.task_mode.value,
            "asset_ids": list(self.asset_ids),
            "subject_definitions": [item.to_wire() for item in self.subject_definitions],
            "reference_definitions": [item.to_wire() for item in self.reference_definitions],
            "global_fields": [item.to_wire() for item in self.global_fields],
            "timed_spans": [item.to_wire() for item in self.timed_spans],
            "census": [item.to_wire() for item in self.census],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @classmethod
    def from_wire(cls, value: object) -> ProductionSemanticSliceV1:
        wire = _closed(
            value,
            "semantic_slice_wire",
            frozenset(
                {
                    "schema",
                    "segment_id",
                    "global_start_milliseconds",
                    "global_end_milliseconds",
                    "task_mode",
                    "asset_ids",
                    "subject_definitions",
                    "reference_definitions",
                    "global_fields",
                    "timed_spans",
                    "census",
                }
            ),
        )
        if wire["schema"] != PRODUCTION_SEMANTIC_SLICE_SCHEMA:
            raise ProductionSemanticError("semantic_slice_schema")
        try:
            task_mode = TaskMode(wire["task_mode"])
        except (TypeError, ValueError) as exc:
            raise ProductionSemanticError("semantic_slice_task_mode") from exc
        return cls(
            segment_id=cast(str, wire["segment_id"]),
            global_start_milliseconds=cast(int, wire["global_start_milliseconds"]),
            global_end_milliseconds=cast(int, wire["global_end_milliseconds"]),
            task_mode=task_mode,
            asset_ids=_ids(wire["asset_ids"], "semantic_slice_asset_ids"),
            subject_definitions=tuple(
                SubjectDefinitionV1.from_wire(item)
                for item in _bounded_rows(wire["subject_definitions"], "semantic_slice_subjects")
            ),
            reference_definitions=tuple(
                ReferenceDefinitionV1.from_wire(item)
                for item in _bounded_rows(
                    wire["reference_definitions"], "semantic_slice_references"
                )
            ),
            global_fields=tuple(
                SemanticFieldV1.from_wire(item)
                for item in _bounded_rows(wire["global_fields"], "semantic_slice_globals")
            ),
            timed_spans=tuple(
                TimedSemanticSpanV1.from_wire(item)
                for item in _bounded_rows(wire["timed_spans"], "semantic_slice_timed_spans")
            ),
            census=tuple(
                SemanticCensusEntryV1.from_wire(item)
                for item in _bounded_rows(
                    wire["census"], "semantic_slice_census", MAX_SEMANTIC_ITEMS * 4
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticCensusV1:
    entries: tuple[SemanticCensusEntryV1, ...]

    @property
    def complete(self) -> bool:
        return all(
            entry.disposition
            in {SemanticDispositionV1.GLOBAL_CARRIED, SemanticDispositionV1.ASSIGNED_REBASED}
            for entry in self.entries
        )

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or not all(
            type(item) is SemanticCensusEntryV1 for item in self.entries
        ):
            raise ProductionSemanticError("semantic_census_entries")
        if len(self.entries) > MAX_SEMANTIC_ITEMS * MAX_SEMANTIC_ITEMS:
            raise ProductionSemanticError("semantic_census_entries")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": PRODUCTION_SEMANTIC_CENSUS_SCHEMA,
            "entries": [item.to_wire() for item in self.entries],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    @classmethod
    def from_wire(cls, value: object) -> SemanticCensusV1:
        wire = _closed(value, "semantic_census_wire", frozenset({"schema", "entries"}))
        if wire["schema"] != PRODUCTION_SEMANTIC_CENSUS_SCHEMA:
            raise ProductionSemanticError("semantic_census_schema")
        values = wire["entries"]
        if type(values) is not list or len(cast(list[object], values)) > MAX_SEMANTIC_ITEMS**2:
            raise ProductionSemanticError("semantic_census_entries")
        return cls(tuple(SemanticCensusEntryV1.from_wire(item) for item in values))


@dataclass(frozen=True, slots=True)
class ProductionLocalShotV1:
    shot_id: str
    ordinal: int
    start_milliseconds: int
    prose: str
    exact_dialogue: tuple[str, ...] = ()
    visible_text: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _id(self.shot_id, "production_local_shot_id")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_SEMANTIC_ITEMS:
            raise ProductionSemanticError("production_local_shot_ordinal")
        _millisecond(self.start_milliseconds, "production_local_shot_start")
        _text(self.prose, "production_local_shot_prose")
        for values, field in (
            (self.exact_dialogue, "production_local_shot_dialogue"),
            (self.visible_text, "production_local_shot_visible_text"),
        ):
            if type(values) is not tuple or len(values) > MAX_SEMANTIC_ITEMS:
                raise ProductionSemanticError(field)
            for value in values:
                _text(value, field)


def validate_semantic_references(
    authority: ProductionSemanticAuthorityV1,
    *,
    subject_ids: tuple[str, ...] | None = None,
    asset_ids: tuple[str, ...] | None = None,
    target_asset_ids: tuple[str, ...] | None = None,
) -> None:
    """Fail when semantic references are unknown or required content is unavailable locally."""

    if not isinstance(authority, ProductionSemanticAuthorityV1):
        raise ProductionSemanticError("semantic_authority")
    # `subject_ids`/`asset_ids` are the enclosing planning registries. Definitions enrich those
    # stable IDs; they are not required to duplicate a client-safe context projection.
    known_subjects = {item.subject_id for item in authority.subject_definitions} | set(
        subject_ids or ()
    )
    known_assets = {item.asset_id for item in authority.reference_definitions} | set(
        asset_ids or ()
    )
    if subject_ids is not None and not {
        item.subject_id for item in authority.subject_definitions
    }.issubset(subject_ids):
        raise ProductionSemanticError("semantic_subject_registry_mismatch")
    if asset_ids is not None and not {
        item.asset_id for item in authority.reference_definitions
    }.issubset(asset_ids):
        raise ProductionSemanticError("semantic_asset_registry_mismatch")
    referenced_subjects: set[str] = set()
    referenced_assets: set[str] = set()
    for field in authority.global_fields:
        referenced_subjects.update(field.subject_ids)
        referenced_assets.update(field.asset_ids)
    for span in authority.timed_spans:
        referenced_subjects.update(span.subject_ids)
        referenced_assets.update(span.asset_ids)
    for subject in authority.subject_definitions:
        referenced_assets.update(subject.source_asset_ids)
    if referenced_subjects - known_subjects:
        raise ProductionSemanticError("semantic_subject_reference_unknown")
    if referenced_assets - known_assets:
        raise ProductionSemanticError("semantic_asset_reference_unknown")
    if target_asset_ids is not None:
        target = set(_ids(target_asset_ids, "semantic_target_asset_ids"))
        if not target.issubset(known_assets):
            raise ProductionSemanticError("semantic_target_asset_unknown")
        if referenced_assets - target:
            raise ProductionSemanticError("semantic_local_asset_unavailable")


def parse_canonical_semantics(text: str) -> ProductionSemanticAuthorityV1:
    """Extract canonical non-storyboard sections; prose never becomes timing authority."""

    value = _text(text, "canonical_semantic_text")
    if "\n\n" not in value and "[Shot 1]" in value and _CANONICAL_BLOCK.fullmatch(value) is None:
        return ProductionSemanticAuthorityV1()
    fields: list[SemanticFieldV1] = []
    blocks = value.split("\n\n")
    saw_section = False
    for index, block in enumerate(blocks):
        match = _CANONICAL_BLOCK.fullmatch(block)
        if match is None:
            if index == 0 and not saw_section:
                fields.append(
                    SemanticFieldV1("alignment_instruction", "alignment_instruction", block)
                )
                continue
            raise ProductionSemanticError("canonical_semantic_block")
        saw_section = True
        heading = match.group("heading")
        if heading not in _CANONICAL_HEADINGS:
            raise ProductionSemanticError("canonical_semantic_unknown_section")
        if heading in _STORYBOARD_HEADINGS:
            continue
        fields.append(
            SemanticFieldV1(
                field_id=f"canonical_{heading}",
                heading=heading,
                text=match.group("body"),
            )
        )
    if not saw_section:
        raise ProductionSemanticError("canonical_semantic_sections_missing")
    return ProductionSemanticAuthorityV1(global_fields=tuple(fields))


def clip_derived_semantics_removed(
    authority: ProductionSemanticAuthorityV1, *, typed_definitions: bool
) -> ProductionSemanticAuthorityV1:
    """Drop the canonical sections a renderer derives per clip from a parsed document."""

    # GUARD: two rendered sections are statements about one clip, not source content. The keyframe
    # alignment sentence names that clip's duration mark, picture numbers and shot count; the
    # subject definitions are re-rendered for each clip from the typed definitions. Carrying
    # either into a production plan repeats one clip's facts in every segment, and re-reading
    # either from the candidate at admission contradicts the retained authority (the alignment
    # field merges unbound against a bound copy, the definitions disagree with the typed ones), so
    # a frame-anchored or full-reference Context is refused. Every reader of a canonical document
    # that becomes production authority must pass through here; do not filter at one call site.
    derived = {"alignment_instruction"}
    if typed_definitions:
        derived.add("subject_definitions")
    return replace(
        authority,
        global_fields=tuple(
            item for item in authority.global_fields if item.heading not in derived
        ),
    )


def canonical_text_asset_ids(
    text: str, authority: ProductionSemanticAuthorityV1
) -> tuple[str, ...]:
    """Bind explicit canonical labels to the one retained reference registry."""
    labels = {item.backend_label: item.asset_id for item in authority.reference_definitions}
    tokens = tuple(dict.fromkeys(_REFERENCE_TOKEN.findall(text)))
    if any(token not in labels for token in tokens):
        raise ProductionSemanticError("canonical_reference_binding_missing")
    return tuple(labels[token] for token in tokens)


def canonical_text_subject_ids(
    text: str, authority: ProductionSemanticAuthorityV1
) -> tuple[str, ...]:
    """Bind canonical subject labels without renumbering a sliced authority."""

    labels = {
        f"<Subject {item.connection_order}>": item.subject_id
        for item in authority.subject_definitions
    }
    tokens = tuple(dict.fromkeys(_SUBJECT_TOKEN.findall(text)))
    if any(token not in labels for token in tokens):
        raise ProductionSemanticError("canonical_subject_binding_missing")
    return tuple(labels[token] for token in tokens)


def bind_canonical_semantic_references(
    authority: ProductionSemanticAuthorityV1,
) -> ProductionSemanticAuthorityV1:
    # CRITICAL: copying a label-bearing preamble/audio section as an unscoped global field
    # leaks unavailable image references into later T2VA cuts. Retain its actual asset scope.
    return replace(
        authority,
        global_fields=tuple(
            replace(
                item,
                subject_ids=tuple(
                    dict.fromkeys(
                        (*item.subject_ids, *canonical_text_subject_ids(item.text, authority))
                    )
                ),
                asset_ids=tuple(
                    dict.fromkeys(
                        (*item.asset_ids, *canonical_text_asset_ids(item.text, authority))
                    )
                ),
            )
            for item in authority.global_fields
        ),
        timed_spans=tuple(
            replace(
                item,
                subject_ids=tuple(
                    dict.fromkeys(
                        (*item.subject_ids, *canonical_text_subject_ids(item.text, authority))
                    )
                ),
                asset_ids=tuple(
                    dict.fromkeys(
                        (*item.asset_ids, *canonical_text_asset_ids(item.text, authority))
                    )
                ),
            )
            for item in authority.timed_spans
        ),
    )


def merge_semantic_authorities(
    *authorities: ProductionSemanticAuthorityV1,
) -> ProductionSemanticAuthorityV1:
    """Merge equal-ID rows once and fail closed on contradictions."""

    def merge_rows(values: tuple[tuple[object, ...], ...], key: str) -> tuple[object, ...]:
        result: list[object] = []
        by_id: dict[str, object] = {}
        for rows in values:
            for row in rows:
                identifier = cast(str, getattr(row, key))
                existing = by_id.get(identifier)
                if existing is None:
                    by_id[identifier] = row
                    result.append(row)
                elif existing != row:
                    raise ProductionSemanticError(f"semantic_merge_conflict:{identifier}")
        return tuple(result)

    for authority in authorities:
        if not isinstance(authority, ProductionSemanticAuthorityV1):
            raise ProductionSemanticError("semantic_merge_authority")
    return ProductionSemanticAuthorityV1(
        subject_definitions=cast(
            tuple[SubjectDefinitionV1, ...],
            merge_rows(tuple(item.subject_definitions for item in authorities), "subject_id"),
        ),
        reference_definitions=cast(
            tuple[ReferenceDefinitionV1, ...],
            merge_rows(tuple(item.reference_definitions for item in authorities), "asset_id"),
        ),
        global_fields=cast(
            tuple[SemanticFieldV1, ...],
            merge_rows(tuple(item.global_fields for item in authorities), "field_id"),
        ),
        timed_spans=cast(
            tuple[TimedSemanticSpanV1, ...],
            merge_rows(tuple(item.timed_spans for item in authorities), "span_id"),
        ),
        required_cut_milliseconds=tuple(
            sorted({cut for item in authorities for cut in item.required_cut_milliseconds})
        ),
        forbidden_cut_intervals_milliseconds=tuple(
            sorted(
                {
                    interval
                    for item in authorities
                    for interval in item.forbidden_cut_intervals_milliseconds
                }
            )
        ),
    )


def _decimal_milliseconds(value: Decimal, field: str) -> int:
    milliseconds = value * 1000
    if milliseconds != milliseconds.to_integral_value():
        raise ProductionSemanticError(field)
    return _millisecond(int(milliseconds), field)


def _rendered_milliseconds(value: Decimal, field: str) -> int:
    """Project graph time through the canonical renderer's three-decimal time precision."""

    # GUARD: graph durations may be fractional milliseconds while the rendered source authority
    # is exactly millisecond-shaped. Use the renderer's ROUND_HALF_UP rule; truncation disagrees
    # with the accepted prompt clock and exact-only handling would reject valid canonical plans.
    milliseconds = (value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return _millisecond(int(milliseconds), field)


def _canonical_source_shots(value: str) -> tuple[tuple[str, int | None], ...]:
    """Return each rendered shot body with the cut time its marker carries, if any."""

    matches = tuple(_SHOT_MARKER.finditer(value))
    ordinals = tuple(int(match.group("ordinal")) for match in matches)
    if not matches or ordinals != tuple(range(1, len(matches) + 1)):
        raise ProductionSemanticError("semantic_source_storyboard_shape")
    shots: list[tuple[str, int | None]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        prefix = value[: match.start()].strip() if index == 0 else ""
        body = value[match.end() : end].strip()
        combined = " ".join(part for part in (prefix, body) if part)
        if not combined:
            raise ProductionSemanticError("semantic_source_storyboard_body")
        start = (
            None
            if match.group("minute") is None
            else int(match.group("minute")) * 60_000
            + int(match.group("second")) * 1000
            + int(match.group("millisecond"))
        )
        # The first shot owns the clip start and every later shot names its own cut.
        if (start is None) != (index == 0):
            raise ProductionSemanticError("semantic_source_storyboard_shape")
        shots.append((combined, start))
    return tuple(shots)


def _source_shot_intervals(
    shots: tuple[tuple[str, int | None], ...],
    segments: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int, int], ...]:
    """Place each rendered shot on the source clock as (segment index, start, end)."""

    if len(shots) == len(segments):
        return tuple((index, start, end) for index, (start, end) in enumerate(segments))
    if len(segments) != 1:
        raise ProductionSemanticError("semantic_source_storyboard_shape")
    # GUARD: the product's intent-graph producers emit one typed segment, while the rendered
    # description may carry several shots written in the official `[Shot N] At MM:SS.mmm,` form
    # (user intent or an accepted assisted candidate). Requiring one typed segment per marker
    # refuses every such Context at prepare. Lift the markers instead: their rendered cut times
    # are the source clock. Do not fall back to one span holding the whole description, which
    # would re-emit nested `[Shot N]` markers inside a timed sentence.
    clip_start, clip_end = segments[0]
    starts = [clip_start, *(cast(int, start) for _body, start in shots[1:])]
    if any(later <= earlier for earlier, later in zip(starts, starts[1:], strict=False)):
        raise ProductionSemanticError("semantic_source_storyboard_shape")
    if starts[-1] >= clip_end:
        raise ProductionSemanticError("semantic_source_storyboard_shape")
    return tuple(
        (0, start, starts[index + 1] if index + 1 < len(starts) else clip_end)
        for index, start in enumerate(starts)
    )


def _source_clock_end(context_plan: ContextPlan, rendered_end: int) -> int:
    """Return the end of the source clip on the requested whole-second clock."""

    request = context_plan.request
    seconds = (
        request.effective_duration_seconds
        if request.requested_duration_seconds is None
        else request.requested_duration_seconds
    )
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or seconds != int(seconds)
    ):
        return rendered_end
    # GUARD: Production allocates segments in requested whole seconds, but the H3 frame lattice
    # makes the effective clip slightly longer (10 s is 243 frames, 10.125 s). Carrying that
    # surplus onto the production timeline leaves a sliver span in the next segment and puts the
    # source's own end strictly inside an immutable span, which forbids the cut at that second.
    return min(rendered_end, int(seconds) * 1000)


def derive_production_semantic_authority(
    context_plan: ContextPlan,
    prompt_document: PromptDocument,
) -> ProductionSemanticAuthorityV1:
    """Derive authority from one server-owned typed plan and its exact rendered document."""

    if not isinstance(context_plan, ContextPlan) or not isinstance(prompt_document, PromptDocument):
        raise ProductionSemanticError("semantic_source_type")
    if prompt_document.plan_id != context_plan.plan_id:
        raise ProductionSemanticError("semantic_source_plan_id")
    if (
        prompt_document.profile != context_plan.request.profile
        or prompt_document.task_mode is not context_plan.request.task_mode
    ):
        raise ProductionSemanticError("semantic_source_profile")
    subjects = tuple(
        SubjectDefinitionV1(
            subject_id=item.subject_id,
            label=item.label,
            connection_order=index,
            description=item.description,
            source_asset_ids=item.source_asset_ids,
        )
        for index, item in enumerate(context_plan.intent_graph.subjects, start=1)
    )
    references = tuple(
        ReferenceDefinitionV1(
            asset_id=asset.asset_id,
            kind=asset.kind,
            role=asset.role,
            connection_order=asset.connection_order,
            backend_label=context_plan.request.reference_registry.label_for(asset.asset_id).label,
        )
        for asset in context_plan.request.reference_registry.assets
    )
    # The typed definitions below are the canonical source for per-cut reference availability.
    # Keeping the rendered aggregate as an unscoped global would both duplicate it and leak
    # unavailable labels into a local mode that does not own those assets.
    globals_authority = clip_derived_semantics_removed(
        parse_canonical_semantics(prompt_document.text),
        typed_definitions=bool(subjects or references),
    )
    description = next(
        (
            section.body
            for section in prompt_document.sections
            if section.heading in _STORYBOARD_HEADINGS
        ),
        None,
    )
    if description is None:
        raise ProductionSemanticError("semantic_source_storyboard_section")
    source_end = _rendered_milliseconds(
        context_plan.intent_graph.effective_duration.seconds, "semantic_source_duration"
    )
    clock_end = _source_clock_end(context_plan, source_end)
    spans: list[TimedSemanticSpanV1] = []
    exact_texts = tuple(
        value.text
        for value in context_plan.hard_constraints.exact_texts
        if isinstance(value, ExactTextConstraint)
    )
    # GUARD: a rendered description is timed authority only while it carries typed positive
    # constraints (exact text, required content, keep/change, timing) that segment materialization
    # must find again. Plain prose is storyboard content: the canonical shots or the reviewed rows
    # own it. Emitting it as a timed span as well repeats it in every canonical segment and
    # injects the single-clip description, at single-clip times, into reviewed whole-video rows.
    # For the same reason a plain description's shot markers are not read here at all: nothing
    # downstream places them, so their shape must not decide whether the Context can be planned.
    carries_typed_content = any(
        not isinstance(value, ForbiddenContent)
        for value in context_plan.hard_constraints.constraints
    )
    source_shots: tuple[tuple[str, int | None], ...] = ()
    shot_intervals: tuple[tuple[int, int, int], ...] = ()
    if carries_typed_content:
        source_shots = _canonical_source_shots(description)
        shot_intervals = _source_shot_intervals(
            source_shots,
            tuple(
                (
                    min(
                        _rendered_milliseconds(segment.start.seconds, "semantic_source_start"),
                        clock_end,
                    ),
                    min(
                        _rendered_milliseconds(segment.end.seconds, "semantic_source_end"),
                        clock_end,
                    ),
                )
                for segment in context_plan.intent_graph.segments
            ),
        )
    for index, ((shot_body, _cut), (segment_index, start, end)) in enumerate(
        zip(source_shots, shot_intervals, strict=True), start=1
    ):
        if end <= start:
            raise ProductionSemanticError("semantic_source_storyboard_shape")
        segment = context_plan.intent_graph.segments[segment_index]
        spans.append(
            TimedSemanticSpanV1(
                span_id=f"source_segment_{index}",
                kind="canonical_shot",
                start_milliseconds=start,
                end_milliseconds=end,
                text=shot_body,
                subject_ids=segment.subject_ids,
                asset_ids=tuple(
                    dict.fromkeys(
                        asset_id
                        for subject in context_plan.intent_graph.subjects
                        if subject.subject_id in segment.subject_ids
                        for asset_id in subject.source_asset_ids
                    )
                ),
                split_policy=(
                    SemanticSplitPolicyV1.IMMUTABLE
                    if any(value in shot_body for value in exact_texts)
                    else SemanticSplitPolicyV1.CLIP
                ),
            )
        )
    cuts: set[int] = set()
    intervals: set[tuple[int, int]] = set()
    for timing in context_plan.hard_constraints.timings:
        start = _decimal_milliseconds(timing.start.seconds, "semantic_timing_start")
        timing_end = (
            None
            if timing.end is None
            else _decimal_milliseconds(timing.end.seconds, "semantic_timing_end")
        )
        if start > source_end or (timing_end is not None and timing_end > source_end):
            raise ProductionSemanticError("semantic_timing_outside_source")
        if timing_end is None or timing_end == start:
            # IMPORTANT: an event time is not a cut request. The current span contract cannot
            # safely rebase a zero-width event, so require explicit review instead of inventing
            # either a mandatory cut or a duration around it.
            raise ProductionSemanticError("semantic_timing_point_requires_review")
        timing_end = min(timing_end, clock_end)
        if start >= timing_end:
            raise ProductionSemanticError("semantic_timing_outside_source")
        intervals.add((start, timing_end))
    merged = merge_semantic_authorities(
        ProductionSemanticAuthorityV1(
            subject_definitions=subjects,
            reference_definitions=references,
            timed_spans=tuple(spans),
            required_cut_milliseconds=tuple(sorted(cuts)),
            forbidden_cut_intervals_milliseconds=tuple(sorted(intervals)),
        ),
        globals_authority,
    )
    return bind_canonical_semantic_references(merged)


def slice_production_semantics(
    authority: ProductionSemanticAuthorityV1,
    *,
    segment_id: str,
    global_start_milliseconds: int,
    global_end_milliseconds: int,
    task_mode: TaskMode,
    asset_ids: tuple[str, ...],
) -> ProductionSemanticSliceV1:
    """Carry global fields and rebase overlapping timed fields onto one local half-open clock."""

    _id(segment_id, "semantic_slice_segment_id")
    start = _millisecond(global_start_milliseconds, "semantic_slice_start")
    end = _millisecond(global_end_milliseconds, "semantic_slice_end", allow_zero=False)
    if end <= start:
        raise ProductionSemanticError("semantic_slice_interval")
    target_assets = _ids(asset_ids, "semantic_slice_asset_ids")
    target_asset_set = set(target_assets)
    subject_source_assets = {
        item.subject_id: frozenset(item.source_asset_ids) for item in authority.subject_definitions
    }

    def required_assets(
        subject_ids: tuple[str, ...], direct_asset_ids: tuple[str, ...]
    ) -> set[str]:
        return set(direct_asset_ids).union(
            *(subject_source_assets.get(subject_id, frozenset()) for subject_id in subject_ids)
        )

    local_references = tuple(
        item for item in authority.reference_definitions if item.asset_id in target_asset_set
    )
    local_subjects = tuple(
        item
        for item in authority.subject_definitions
        if not item.source_asset_ids or set(item.source_asset_ids).issubset(target_asset_set)
    )
    local_spans: list[TimedSemanticSpanV1] = []
    census = [
        SemanticCensusEntryV1(item.subject_id, SemanticDispositionV1.GLOBAL_CARRIED, segment_id)
        for item in local_subjects
    ]
    census.extend(
        SemanticCensusEntryV1(item.asset_id, SemanticDispositionV1.GLOBAL_CARRIED, segment_id)
        for item in local_references
    )
    local_globals: list[SemanticFieldV1] = []
    for item in authority.global_fields:
        if required_assets(item.subject_ids, item.asset_ids).issubset(target_asset_set):
            local_globals.append(item)
            census.append(
                SemanticCensusEntryV1(
                    item.field_id, SemanticDispositionV1.GLOBAL_CARRIED, segment_id
                )
            )
        else:
            census.append(
                SemanticCensusEntryV1(item.field_id, SemanticDispositionV1.REJECTED, segment_id)
            )
    for span in authority.timed_spans:
        if span.start_milliseconds >= end or span.end_milliseconds <= start:
            continue
        clipped_start = max(span.start_milliseconds, start)
        clipped_end = min(span.end_milliseconds, end)
        crosses = clipped_start != span.start_milliseconds or clipped_end != span.end_milliseconds
        missing_assets = required_assets(span.subject_ids, span.asset_ids) - target_asset_set
        if missing_assets:
            census.append(
                SemanticCensusEntryV1(span.span_id, SemanticDispositionV1.REJECTED, segment_id)
            )
            continue
        if crosses and span.split_policy is SemanticSplitPolicyV1.IMMUTABLE:
            census.append(
                SemanticCensusEntryV1(
                    span.span_id, SemanticDispositionV1.REVIEW_REQUIRED, segment_id
                )
            )
            continue
        local_start = clipped_start - start
        local_end = clipped_end - start
        # CRITICAL: both source and segment IDs may already fill the identifier bound. Raw
        # concatenation rejects representable content; retain the source ID separately and bind
        # the local identity to the unambiguous pair with a fixed-length digest.
        local_identity = canonical_fingerprint(
            {"source_span_id": span.span_id, "segment_id": segment_id}
        )
        local_spans.append(
            TimedSemanticSpanV1(
                span_id=f"slice_{local_identity.removeprefix('sha256:')}",
                source_span_id=span.span_id,
                kind=span.kind,
                start_milliseconds=local_start,
                end_milliseconds=local_end,
                text=span.text,
                subject_ids=span.subject_ids,
                asset_ids=span.asset_ids,
                split_policy=span.split_policy,
            )
        )
        census.append(
            SemanticCensusEntryV1(
                span.span_id,
                SemanticDispositionV1.ASSIGNED_REBASED,
                segment_id,
                local_start,
                local_end,
            )
        )
    return ProductionSemanticSliceV1(
        segment_id=segment_id,
        global_start_milliseconds=start,
        global_end_milliseconds=end,
        task_mode=task_mode,
        asset_ids=target_assets,
        subject_definitions=local_subjects,
        reference_definitions=local_references,
        global_fields=tuple(local_globals),
        timed_spans=tuple(local_spans),
        census=tuple(census),
    )


def assert_complete_semantic_census(
    authority: ProductionSemanticAuthorityV1,
    slices: tuple[ProductionSemanticSliceV1, ...],
) -> SemanticCensusV1:
    """Require every source field to be carried or assigned without unresolved dispositions."""

    # CRITICAL: IDs and a recomputed hash do not prove preservation. Re-derive every slice
    # from the source authority before trusting its content, clock, references or census.
    for item in slices:
        expected = slice_production_semantics(
            authority,
            segment_id=item.segment_id,
            global_start_milliseconds=item.global_start_milliseconds,
            global_end_milliseconds=item.global_end_milliseconds,
            task_mode=item.task_mode,
            asset_ids=item.asset_ids,
        )
        if item != expected:
            raise ProductionSemanticError("semantic_slice_source_mismatch")
    entries = tuple(entry for item in slices for entry in item.census)
    for entry in entries:
        if entry.disposition in {
            SemanticDispositionV1.REVIEW_REQUIRED,
            SemanticDispositionV1.REJECTED,
        }:
            raise ProductionSemanticError("semantic_preservation_incomplete")
    global_ids = (
        {item.subject_id for item in authority.subject_definitions}
        | {item.asset_id for item in authority.reference_definitions}
        | {item.field_id for item in authority.global_fields}
    )
    timed_ids = {item.span_id for item in authority.timed_spans}
    seen_global = {
        entry.source_id
        for entry in entries
        if entry.disposition is SemanticDispositionV1.GLOBAL_CARRIED
    }
    seen_timed = {
        entry.source_id
        for entry in entries
        if entry.disposition is SemanticDispositionV1.ASSIGNED_REBASED
    }
    if global_ids - seen_global or timed_ids - seen_timed:
        raise ProductionSemanticError("semantic_preservation_incomplete")
    for span in authority.timed_spans:
        intervals = sorted(
            (
                item.global_start_milliseconds + local.start_milliseconds,
                item.global_start_milliseconds + local.end_milliseconds,
            )
            for item in slices
            for local in item.timed_spans
            if local.source_span_id == span.span_id
        )
        cursor = span.start_milliseconds
        for start, end in intervals:
            if start != cursor:
                raise ProductionSemanticError("semantic_timed_coverage")
            cursor = end
        if cursor != span.end_milliseconds:
            raise ProductionSemanticError("semantic_timed_coverage")
    return SemanticCensusV1(entries)


__all__ = [
    "MAX_SEMANTIC_ITEMS",
    "MAX_SEMANTIC_TOTAL_TEXT_LENGTH",
    "ProductionLocalShotV1",
    "ProductionSemanticAuthorityV1",
    "ProductionSemanticError",
    "ProductionSemanticSliceV1",
    "ReferenceDefinitionV1",
    "SemanticCensusEntryV1",
    "SemanticCensusV1",
    "SemanticDispositionV1",
    "SemanticFieldV1",
    "SemanticSplitPolicyV1",
    "SubjectDefinitionV1",
    "TimedSemanticSpanV1",
    "assert_complete_semantic_census",
    "bind_canonical_semantic_references",
    "canonical_text_asset_ids",
    "canonical_text_subject_ids",
    "clip_derived_semantics_removed",
    "derive_production_semantic_authority",
    "merge_semantic_authorities",
    "parse_canonical_semantics",
    "slice_production_semantics",
    "validate_semantic_references",
]
