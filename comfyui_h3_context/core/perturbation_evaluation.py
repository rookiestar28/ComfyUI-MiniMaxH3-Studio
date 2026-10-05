"""Deterministic one-variable perturbation studies with explicit oracle missingness.

This module is a pure structural evaluation boundary.  It never calls a provider, reads media,
or infers official behavior.  Every portable case is regenerated from its source and spec so
caller-declared variants, fingerprints, diffs, summaries, or oracle states are not trusted.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import cast

from .canonical import canonical_fingerprint

PERTURBATION_EVALUATION_SCHEMA = "h3.context.perturbation.evaluation.v1"
PERTURBATION_CORPUS_VERSION = "1.0.0"
FROZEN_CORPUS_FINGERPRINT = (
    "sha256:96cfebbae82b1decab9cfa352f5ddd6151596576b9a094d2890c782a5c998c25"
)
MAX_MATERIAL_DEPTH = 8
MAX_MATERIAL_BYTES = 32_768
MAX_MATERIAL_MEMBERS = 64
MAX_MATERIAL_LIST_ITEMS = 64
MAX_MATERIAL_STRING_LENGTH = 1_024
FROZEN_SOURCE_CLUSTER_COUNT = 9
FROZEN_CASE_COUNT = 27

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TARGET_PATH = re.compile(r"/[a-z][a-z0-9_]{0,63}\Z")
_MATERIAL_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")
_SENSITIVE_KEY_SEGMENTS = frozenset(
    {
        "api",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "path",
        "secret",
        "signed",
        "token",
        "url",
    }
)
_UNSAFE_VALUE_MARKERS = (
    "http://",
    "https://",
    "file://",
    "bearer ",
    "token=",
    "password=",
    "api_key=",
)


class PerturbationError(ValueError):
    """Raised when a perturbation contract or derived wire is invalid."""


class CorpusPartition(str, Enum):
    """Closed local partitions; neither represents an oracle partition."""

    DEVELOPMENT = "development"
    FROZEN_TEST = "frozen_test"


class PerturbationDimension(str, Enum):
    """Exact bounded M14-02 one-variable study inventory."""

    WORDING = "wording"
    UNICODE = "unicode"
    METADATA = "metadata"
    ASSET_ORDER = "asset_order"
    ASSET_ROLE = "asset_role"
    MODALITY_DUPLICATE = "modality_duplicate"
    MODALITY_REMOVAL = "modality_removal"
    MODALITY_REPLACEMENT = "modality_replacement"
    TEMPORAL_SHIFT = "temporal_shift"
    TEMPORAL_REVERSE = "temporal_reverse"
    TEMPORAL_SPLICE = "temporal_splice"
    SUBJECT_FUSION = "subject_fusion"
    DIRECTIVE_COPY = "directive_copy"
    DIRECTIVE_RETAIN = "directive_retain"
    DIRECTIVE_ADAPT = "directive_adapt"
    DIRECTIVE_EXCLUDE = "directive_exclude"
    AMBIGUITY = "ambiguity"
    CONFLICT = "conflict"
    DURATION = "duration"
    DIALOGUE = "dialogue"
    VISIBLE_TEXT = "visible_text"
    STYLE = "style"
    MOTION = "motion"
    CAMERA = "camera"
    VIDEO_AUDIO = "video_audio"
    DISTRACTOR = "distractor"
    UNSUPPORTED_INPUT = "unsupported_input"


class PerturbationOperation(str, Enum):
    """Closed generic operations used by the frozen local corpus."""

    REPLACE = "replace"
    REMOVE = "remove"
    APPEND = "append"
    DUPLICATE = "duplicate"
    REORDER = "reorder"
    NUMERIC_SHIFT = "numeric_shift"
    REVERSE = "reverse"
    SPLICE = "splice"


class ExpectedRelationKind(str, Enum):
    """Local expectation only; this is not an observed model/oracle outcome."""

    INVARIANT = "invariant"
    COVARIANT = "covariant"
    MONOTONIC = "monotonic"
    UNKNOWN = "unknown"


class MissingRelationState(str, Enum):
    """The only admitted official/agreement state after M14-01."""

    MISSING = "MISSING"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PerturbationError(f"{field} must be a bounded identifier")
    return value


def _enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise PerturbationError(f"{field} must be {expected.__name__}")


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise PerturbationError(f"{field} must be a canonical SHA-256 fingerprint")
    return value


def _sensitive_key(value: str) -> bool:
    segments = tuple(part for part in re.split(r"[_:.-]+", value.casefold()) if part)
    return any(segment in _SENSITIVE_KEY_SEGMENTS for segment in segments)


def _safe_string(value: str, field: str) -> str:
    if (
        not value
        or len(value) > MAX_MATERIAL_STRING_LENGTH
        or _CONTROL.search(value)
        or _SURROGATE.search(value)
    ):
        raise PerturbationError(f"{field} must be bounded safe text")
    folded = value.casefold()
    if any(marker in folded for marker in _UNSAFE_VALUE_MARKERS) or _WINDOWS_ABSOLUTE.search(value):
        raise PerturbationError(f"{field} contains an unsafe locator")
    if value.startswith(("/", "\\")):
        raise PerturbationError(f"{field} contains an unsafe locator")
    return value


def _freeze_material(value: object, *, field: str, depth: int = 0) -> object:
    if depth > MAX_MATERIAL_DEPTH:
        raise PerturbationError(f"{field} exceeds the material depth limit")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PerturbationError(f"{field} number must be finite")
        return value
    if isinstance(value, str):
        return _safe_string(value, field)
    if isinstance(value, Mapping):
        if len(value) > MAX_MATERIAL_MEMBERS:
            raise PerturbationError(f"{field} exceeds the member limit")
        frozen: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str) or _MATERIAL_KEY.fullmatch(key) is None:
                raise PerturbationError(f"{field} contains an invalid material key")
            if _sensitive_key(key):
                raise PerturbationError(f"{field} contains a sensitive material key")
            frozen[key] = _freeze_material(value[key], field=f"{field}.{key}", depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_MATERIAL_LIST_ITEMS:
            raise PerturbationError(f"{field} exceeds the list limit")
        return tuple(
            _freeze_material(item, field=f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise PerturbationError(f"{field} contains an unsupported material value")


def _thaw_material(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_material(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_material(item) for item in value]
    return value


def _bounded_material(value: object, field: str) -> Mapping[str, object]:
    frozen = _freeze_material(value, field=field)
    if not isinstance(frozen, Mapping) or not frozen:
        raise PerturbationError(f"{field} must be a non-empty object")
    encoded = json.dumps(
        _thaw_material(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_MATERIAL_BYTES:
        raise PerturbationError(f"{field} exceeds the material byte limit")
    return cast(Mapping[str, object], frozen)


def _material_fingerprint(value: Mapping[str, object]) -> str:
    return canonical_fingerprint(_thaw_material(value))


@dataclass(frozen=True, slots=True)
class ExpectedRelation:
    """Preregistered local expectation, never an observed official relation."""

    kind: ExpectedRelationKind
    direction: str | None
    rationale_code: str

    def __post_init__(self) -> None:
        _enum(self.kind, ExpectedRelationKind, "expected relation kind")
        _identifier(self.rationale_code, "expected relation rationale_code")
        if self.kind is ExpectedRelationKind.MONOTONIC:
            if self.direction not in {"increase", "decrease"}:
                raise PerturbationError("monotonic relation requires increase/decrease direction")
        elif self.direction is not None:
            raise PerturbationError("non-monotonic relation direction must be null")

    def to_wire(self) -> dict[str, object]:
        return {
            "state": "EXPECTED",
            "kind": self.kind.value,
            "direction": self.direction,
            "rationale_code": self.rationale_code,
        }


@dataclass(frozen=True, slots=True)
class MissingRelation:
    """Explicit absence object for official relation and agreement lanes."""

    state: MissingRelationState = MissingRelationState.MISSING
    value: None = None

    def __post_init__(self) -> None:
        if self.state is not MissingRelationState.MISSING or self.value is not None:
            raise PerturbationError("official relation and agreement must remain MISSING")

    def to_wire(self) -> dict[str, object]:
        return {"state": self.state.value, "value": None}


@dataclass(frozen=True, slots=True)
class PerturbationSource:
    """One immutable synthetic source owned by exactly one source cluster and partition."""

    source_id: str
    source_cluster_id: str
    partition: CorpusPartition
    material: Mapping[str, object]

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.source_cluster_id, "source_cluster_id")
        _enum(self.partition, CorpusPartition, "source partition")
        object.__setattr__(self, "material", _bounded_material(self.material, "source material"))

    @property
    def fingerprint(self) -> str:
        return _material_fingerprint(self.material)

    def to_wire(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_cluster_id": self.source_cluster_id,
            "partition": self.partition.value,
            "material": _thaw_material(self.material),
            "source_fingerprint": self.fingerprint,
        }


_DIMENSION_RULES: Mapping[
    PerturbationDimension, tuple[PerturbationOperation, str, ExpectedRelationKind]
] = MappingProxyType(
    {
        PerturbationDimension.WORDING: (
            PerturbationOperation.REPLACE,
            "/wording",
            ExpectedRelationKind.INVARIANT,
        ),
        PerturbationDimension.UNICODE: (
            PerturbationOperation.REPLACE,
            "/visible_text",
            ExpectedRelationKind.INVARIANT,
        ),
        PerturbationDimension.METADATA: (
            PerturbationOperation.REPLACE,
            "/metadata",
            ExpectedRelationKind.INVARIANT,
        ),
        PerturbationDimension.ASSET_ORDER: (
            PerturbationOperation.REORDER,
            "/assets",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.ASSET_ROLE: (
            PerturbationOperation.REPLACE,
            "/assets",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.MODALITY_DUPLICATE: (
            PerturbationOperation.DUPLICATE,
            "/assets",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.MODALITY_REMOVAL: (
            PerturbationOperation.SPLICE,
            "/assets",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.MODALITY_REPLACEMENT: (
            PerturbationOperation.REPLACE,
            "/assets",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.TEMPORAL_SHIFT: (
            PerturbationOperation.NUMERIC_SHIFT,
            "/timeline_offset_ms",
            ExpectedRelationKind.MONOTONIC,
        ),
        PerturbationDimension.TEMPORAL_REVERSE: (
            PerturbationOperation.REVERSE,
            "/timeline",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.TEMPORAL_SPLICE: (
            PerturbationOperation.SPLICE,
            "/timeline",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.SUBJECT_FUSION: (
            PerturbationOperation.REPLACE,
            "/subjects",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.DIRECTIVE_COPY: (
            PerturbationOperation.REPLACE,
            "/directives",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.DIRECTIVE_RETAIN: (
            PerturbationOperation.REPLACE,
            "/directives",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.DIRECTIVE_ADAPT: (
            PerturbationOperation.REPLACE,
            "/directives",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.DIRECTIVE_EXCLUDE: (
            PerturbationOperation.REMOVE,
            "/directives",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.AMBIGUITY: (
            PerturbationOperation.REPLACE,
            "/ambiguity",
            ExpectedRelationKind.UNKNOWN,
        ),
        PerturbationDimension.CONFLICT: (
            PerturbationOperation.REPLACE,
            "/conflicts",
            ExpectedRelationKind.UNKNOWN,
        ),
        PerturbationDimension.DURATION: (
            PerturbationOperation.NUMERIC_SHIFT,
            "/duration_ms",
            ExpectedRelationKind.MONOTONIC,
        ),
        PerturbationDimension.DIALOGUE: (
            PerturbationOperation.REPLACE,
            "/dialogue",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.VISIBLE_TEXT: (
            PerturbationOperation.REPLACE,
            "/visible_text",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.STYLE: (
            PerturbationOperation.REPLACE,
            "/style",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.MOTION: (
            PerturbationOperation.REPLACE,
            "/motion",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.CAMERA: (
            PerturbationOperation.REPLACE,
            "/camera",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.VIDEO_AUDIO: (
            PerturbationOperation.REPLACE,
            "/video_audio",
            ExpectedRelationKind.COVARIANT,
        ),
        PerturbationDimension.DISTRACTOR: (
            PerturbationOperation.APPEND,
            "/distractors",
            ExpectedRelationKind.INVARIANT,
        ),
        PerturbationDimension.UNSUPPORTED_INPUT: (
            PerturbationOperation.REPLACE,
            "/unsupported_inputs",
            ExpectedRelationKind.UNKNOWN,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class PerturbationSpec:
    """One preregistered operation targeting exactly one top-level canonical path."""

    case_id: str
    parent_id: str
    source_cluster_id: str
    seed: int
    dimension: PerturbationDimension
    operation: PerturbationOperation
    target_path: str
    value: object | None
    indices: tuple[int, ...]
    delta: int | None
    delete_count: int | None
    expected_local_relation: ExpectedRelation

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _identifier(self.parent_id, "parent_id")
        _identifier(self.source_cluster_id, "source_cluster_id")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**31
        ):
            raise PerturbationError("seed must be a bounded non-negative integer")
        _enum(self.dimension, PerturbationDimension, "dimension")
        _enum(self.operation, PerturbationOperation, "operation")
        if (
            not isinstance(self.target_path, str)
            or _TARGET_PATH.fullmatch(self.target_path) is None
        ):
            raise PerturbationError("target_path must be one bounded top-level JSON pointer")
        if not isinstance(self.indices, tuple) or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in self.indices
        ):
            raise PerturbationError("indices must be a tuple of non-negative integers")
        if self.delta is not None and (
            isinstance(self.delta, bool) or not isinstance(self.delta, int) or self.delta == 0
        ):
            raise PerturbationError("delta must be a non-zero integer or null")
        if self.delete_count is not None and (
            isinstance(self.delete_count, bool)
            or not isinstance(self.delete_count, int)
            or self.delete_count < 0
        ):
            raise PerturbationError("delete_count must be a non-negative integer or null")
        if self.value is not None:
            object.__setattr__(self, "value", _freeze_material(self.value, field="transform value"))
        if not isinstance(self.expected_local_relation, ExpectedRelation):
            raise PerturbationError("expected_local_relation must be typed")
        self._validate_parameters()
        expected_operation, expected_path, expected_relation = _DIMENSION_RULES[self.dimension]
        if (
            self.operation is not expected_operation
            or self.target_path != expected_path
            or self.expected_local_relation.kind is not expected_relation
        ):
            raise PerturbationError("dimension, operation, and path relation is invalid")
        if self.operation is PerturbationOperation.NUMERIC_SHIFT:
            expected_direction = "increase" if cast(int, self.delta) > 0 else "decrease"
            if self.expected_local_relation.direction != expected_direction:
                raise PerturbationError("monotonic direction must match numeric shift")

    def _validate_parameters(self) -> None:
        none_basic = not self.indices and self.delta is None and self.delete_count is None
        if self.operation is PerturbationOperation.REPLACE:
            if self.value is None or not none_basic:
                raise PerturbationError("replace parameters are invalid")
        elif self.operation is PerturbationOperation.REMOVE:
            if self.value is not None or not none_basic:
                raise PerturbationError("remove parameters are invalid")
        elif self.operation is PerturbationOperation.APPEND:
            if self.value is None or not none_basic:
                raise PerturbationError("append parameters are invalid")
        elif self.operation is PerturbationOperation.DUPLICATE:
            if (
                self.value is not None
                or len(self.indices) != 1
                or self.delta is not None
                or self.delete_count is not None
            ):
                raise PerturbationError("duplicate parameters are invalid")
        elif self.operation is PerturbationOperation.REORDER:
            if (
                self.value is not None
                or not self.indices
                or len(set(self.indices)) != len(self.indices)
                or self.delta is not None
                or self.delete_count is not None
            ):
                raise PerturbationError("reorder parameters are invalid")
        elif self.operation is PerturbationOperation.NUMERIC_SHIFT:
            if (
                self.value is not None
                or self.indices
                or self.delta is None
                or self.delete_count is not None
            ):
                raise PerturbationError("numeric shift parameters are invalid")
        elif self.operation is PerturbationOperation.REVERSE:
            if self.value is not None or not none_basic:
                raise PerturbationError("reverse parameters are invalid")
        elif self.operation is PerturbationOperation.SPLICE:
            if (
                not isinstance(self.value, tuple)
                or len(self.indices) != 1
                or self.delta is not None
                or self.delete_count is None
            ):
                raise PerturbationError("splice parameters are invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "parent_id": self.parent_id,
            "source_cluster_id": self.source_cluster_id,
            "seed": self.seed,
            "dimension": self.dimension.value,
            "operation": self.operation.value,
            "target_path": self.target_path,
            "parameters": {
                "value": _thaw_material(self.value),
                "indices": list(self.indices),
                "delta": self.delta,
                "delete_count": self.delete_count,
            },
            "expected_local_relation": self.expected_local_relation.to_wire(),
        }


@dataclass(frozen=True, slots=True, init=False)
class PerturbationCase:
    """Fully regenerated local case with explicit missing official lanes."""

    case_id: str
    parent_id: str
    source_cluster_id: str
    partition: CorpusPartition
    seed: int
    dimension: PerturbationDimension
    operation: PerturbationOperation
    target_path: str
    changed_paths: tuple[str, ...]
    source_fingerprint: str
    variant_fingerprint: str
    variant_material: Mapping[str, object]
    expected_local_relation: ExpectedRelation
    official_relation: MissingRelation = MissingRelation()
    local_official_agreement: MissingRelation = MissingRelation()

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        # CRITICAL: derived cases must stay evaluator-owned; public init bypasses source admission.
        raise PerturbationError("case construction is evaluator-owned")

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case case_id")
        _identifier(self.parent_id, "case parent_id")
        _identifier(self.source_cluster_id, "case source_cluster_id")
        _enum(self.partition, CorpusPartition, "case partition")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**31
        ):
            raise PerturbationError("case seed must be a bounded non-negative integer")
        _enum(self.dimension, PerturbationDimension, "case dimension")
        _enum(self.operation, PerturbationOperation, "case operation")
        if _TARGET_PATH.fullmatch(self.target_path) is None:
            raise PerturbationError("case target_path is invalid")
        if self.changed_paths != (self.target_path,):
            raise PerturbationError("case changed_paths must contain only target_path")
        _fingerprint(self.source_fingerprint, "case source_fingerprint")
        _fingerprint(self.variant_fingerprint, "case variant_fingerprint")
        frozen_variant = _bounded_material(self.variant_material, "case variant material")
        object.__setattr__(self, "variant_material", frozen_variant)
        if self.variant_fingerprint != _material_fingerprint(frozen_variant):
            raise PerturbationError("case variant_fingerprint is not material-derived")
        if self.source_fingerprint == self.variant_fingerprint:
            raise PerturbationError("case source and variant fingerprints must differ")
        if not isinstance(self.expected_local_relation, ExpectedRelation):
            raise PerturbationError("case expected_local_relation must be typed")
        expected_operation, expected_path, expected_kind = _DIMENSION_RULES[self.dimension]
        if (
            self.operation is not expected_operation
            or self.target_path != expected_path
            or self.expected_local_relation.kind is not expected_kind
        ):
            raise PerturbationError("case dimension, operation, path and relation join is invalid")
        if not isinstance(self.official_relation, MissingRelation) or not isinstance(
            self.local_official_agreement, MissingRelation
        ):
            raise PerturbationError("case official relation and agreement must be typed missing")

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "parent_id": self.parent_id,
            "source_cluster_id": self.source_cluster_id,
            "partition": self.partition.value,
            "seed": self.seed,
            "dimension": self.dimension.value,
            "operation": self.operation.value,
            "target_path": self.target_path,
            "changed_paths": list(self.changed_paths),
            "source_fingerprint": self.source_fingerprint,
            "variant_fingerprint": self.variant_fingerprint,
            "variant_material": _thaw_material(self.variant_material),
            "expected_local_relation": self.expected_local_relation.to_wire(),
            "official_relation": self.official_relation.to_wire(),
            "local_official_agreement": self.local_official_agreement.to_wire(),
        }


def _derive_case(
    *,
    case_id: str,
    parent_id: str,
    source_cluster_id: str,
    partition: CorpusPartition,
    seed: int,
    dimension: PerturbationDimension,
    operation: PerturbationOperation,
    target_path: str,
    changed_paths: tuple[str, ...],
    source_fingerprint: str,
    variant_fingerprint: str,
    variant_material: Mapping[str, object],
    expected_local_relation: ExpectedRelation,
) -> PerturbationCase:
    case = object.__new__(PerturbationCase)
    object.__setattr__(case, "case_id", case_id)
    object.__setattr__(case, "parent_id", parent_id)
    object.__setattr__(case, "source_cluster_id", source_cluster_id)
    object.__setattr__(case, "partition", partition)
    object.__setattr__(case, "seed", seed)
    object.__setattr__(case, "dimension", dimension)
    object.__setattr__(case, "operation", operation)
    object.__setattr__(case, "target_path", target_path)
    object.__setattr__(case, "changed_paths", changed_paths)
    object.__setattr__(case, "source_fingerprint", source_fingerprint)
    object.__setattr__(case, "variant_fingerprint", variant_fingerprint)
    object.__setattr__(case, "variant_material", variant_material)
    object.__setattr__(case, "expected_local_relation", expected_local_relation)
    object.__setattr__(case, "official_relation", MissingRelation())
    object.__setattr__(case, "local_official_agreement", MissingRelation())
    case.__post_init__()
    return case


def _recursive_changed_paths(source: object, variant: object, path: str = "") -> tuple[str, ...]:
    if isinstance(source, Mapping) and isinstance(variant, Mapping):
        changes: list[str] = []
        for key in sorted(set(source) | set(variant)):
            child_path = f"{path}/{key}"
            if key not in source or key not in variant:
                changes.append(child_path)
            else:
                changes.extend(_recursive_changed_paths(source[key], variant[key], child_path))
        return tuple(changes)
    if isinstance(source, tuple) and isinstance(variant, tuple):
        if len(source) != len(variant):
            return (path,)
        changes = []
        for index, (source_item, variant_item) in enumerate(zip(source, variant, strict=True)):
            changes.extend(_recursive_changed_paths(source_item, variant_item, f"{path}/{index}"))
        return tuple(changes)
    return () if source == variant else (path,)


def _target_key(path: str) -> str:
    return path[1:]


def apply_perturbation(source: PerturbationSource, spec: PerturbationSpec) -> PerturbationCase:
    """Apply one closed operation and prove exactly one declared top-level path changed."""

    if not isinstance(source, PerturbationSource) or not isinstance(spec, PerturbationSpec):
        raise PerturbationError("source and spec must be typed")
    if source.source_id != spec.parent_id or source.source_cluster_id != spec.source_cluster_id:
        raise PerturbationError("spec parent/source cluster does not match source")
    variant = cast(dict[str, object], _thaw_material(source.material))
    key = _target_key(spec.target_path)
    if key not in variant:
        raise PerturbationError("transform target path is absent from source")
    current = variant[key]

    if spec.operation is PerturbationOperation.REPLACE:
        variant[key] = _thaw_material(spec.value)
    elif spec.operation is PerturbationOperation.REMOVE:
        del variant[key]
    elif spec.operation is PerturbationOperation.APPEND:
        if not isinstance(current, list):
            raise PerturbationError("append target must be a list")
        variant[key] = [*current, _thaw_material(spec.value)]
    elif spec.operation is PerturbationOperation.DUPLICATE:
        if not isinstance(current, list) or spec.indices[0] >= len(current):
            raise PerturbationError("duplicate target/index is invalid")
        index = spec.indices[0]
        variant[key] = [*current[: index + 1], current[index], *current[index + 1 :]]
    elif spec.operation is PerturbationOperation.REORDER:
        if not isinstance(current, list) or set(spec.indices) != set(range(len(current))):
            raise PerturbationError("reorder indices must be an exact list permutation")
        variant[key] = [current[index] for index in spec.indices]
    elif spec.operation is PerturbationOperation.NUMERIC_SHIFT:
        if isinstance(current, bool) or not isinstance(current, int):
            raise PerturbationError("numeric shift target must be an integer")
        variant[key] = current + cast(int, spec.delta)
    elif spec.operation is PerturbationOperation.REVERSE:
        if not isinstance(current, list):
            raise PerturbationError("reverse target must be a list")
        variant[key] = list(reversed(current))
    elif spec.operation is PerturbationOperation.SPLICE:
        if not isinstance(current, list) or spec.indices[0] > len(current):
            raise PerturbationError("splice target/index is invalid")
        start = spec.indices[0]
        delete_count = cast(int, spec.delete_count)
        if start + delete_count > len(current):
            raise PerturbationError("splice delete range exceeds target")
        replacement = cast(list[object], _thaw_material(spec.value))
        variant[key] = [
            *current[:start],
            *replacement,
            *current[start + delete_count :],
        ]

    frozen_variant = _bounded_material(variant, "variant material")
    recursive_changes = _recursive_changed_paths(source.material, frozen_variant)
    if not recursive_changes or any(
        path != spec.target_path and not path.startswith(f"{spec.target_path}/")
        for path in recursive_changes
    ):
        raise PerturbationError("transform must change exactly one declared path")
    changed_paths = (spec.target_path,)
    return _derive_case(
        case_id=spec.case_id,
        parent_id=spec.parent_id,
        source_cluster_id=spec.source_cluster_id,
        partition=source.partition,
        seed=spec.seed,
        dimension=spec.dimension,
        operation=spec.operation,
        target_path=spec.target_path,
        changed_paths=changed_paths,
        source_fingerprint=source.fingerprint,
        variant_fingerprint=_material_fingerprint(frozen_variant),
        variant_material=frozen_variant,
        expected_local_relation=spec.expected_local_relation,
    )


@dataclass(frozen=True, slots=True, init=False)
class PerturbationCorpus:
    """Frozen bounded corpus with evaluator-derived cases and aggregate inventory."""

    corpus_id: str
    corpus_version: str
    sources: tuple[PerturbationSource, ...]
    specs: tuple[PerturbationSpec, ...]
    cases: tuple[PerturbationCase, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        # CRITICAL: derived corpora must stay builder-owned; public init bypasses inventory closure.
        raise PerturbationError("corpus construction is builder-owned")

    def __post_init__(self) -> None:
        _identifier(self.corpus_id, "corpus_id")
        if self.corpus_version != PERTURBATION_CORPUS_VERSION:
            raise PerturbationError("unsupported perturbation corpus version")
        if (
            not isinstance(self.sources, tuple)
            or len(self.sources) != FROZEN_SOURCE_CLUSTER_COUNT
            or not all(isinstance(source, PerturbationSource) for source in self.sources)
        ):
            raise PerturbationError("corpus sources are invalid")
        if (
            not isinstance(self.specs, tuple)
            or len(self.specs) != FROZEN_CASE_COUNT
            or not all(isinstance(spec, PerturbationSpec) for spec in self.specs)
        ):
            raise PerturbationError("corpus specs are invalid")
        if (
            not isinstance(self.cases, tuple)
            or len(self.cases) != FROZEN_CASE_COUNT
            or not all(isinstance(case, PerturbationCase) for case in self.cases)
        ):
            raise PerturbationError("corpus cases are invalid")
        source_map = {source.source_id: source for source in self.sources}
        if len(source_map) != FROZEN_SOURCE_CLUSTER_COUNT:
            raise PerturbationError("corpus source IDs must be unique")
        if tuple(source_map) != tuple(sorted(source_map)):
            raise PerturbationError("corpus sources must use deterministic source_id ordering")
        cluster_ids = tuple(source.source_cluster_id for source in self.sources)
        if len(set(cluster_ids)) != FROZEN_SOURCE_CLUSTER_COUNT:
            raise PerturbationError("corpus source-cluster IDs must be unique")
        case_ids = tuple(spec.case_id for spec in self.specs)
        if case_ids != tuple(sorted(case_ids)) or len(set(case_ids)) != FROZEN_CASE_COUNT:
            raise PerturbationError("corpus specs require unique deterministic case_id ordering")
        dimensions = tuple(spec.dimension for spec in self.specs)
        if len(set(dimensions)) != FROZEN_CASE_COUNT or set(dimensions) != set(
            PerturbationDimension
        ):
            raise PerturbationError("corpus specs must cover every dimension exactly once")
        if {spec.source_cluster_id for spec in self.specs} != set(cluster_ids):
            raise PerturbationError("every frozen source cluster must be represented")
        try:
            regenerated = tuple(
                apply_perturbation(source_map[spec.parent_id], spec) for spec in self.specs
            )
        except KeyError as exc:
            raise PerturbationError("corpus spec references an unknown parent") from exc
        if self.cases != regenerated:
            raise PerturbationError("corpus cases are not evaluator-derived")

    @property
    def source_cluster_count(self) -> int:
        return len({source.source_cluster_id for source in self.sources})

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def official_relation_state(self) -> MissingRelationState:
        return MissingRelationState.MISSING

    @property
    def local_official_agreement_state(self) -> MissingRelationState:
        return MissingRelationState.MISSING

    def _wire_without_fingerprint(self) -> dict[str, object]:
        return {
            "schema": PERTURBATION_EVALUATION_SCHEMA,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "source_cluster_count": self.source_cluster_count,
            "case_count": self.case_count,
            "dimensions": [spec.dimension.value for spec in self.specs],
            "official_relation_state": self.official_relation_state.value,
            "local_official_agreement_state": self.local_official_agreement_state.value,
            "sources": [source.to_wire() for source in self.sources],
            "specs": [spec.to_wire() for spec in self.specs],
            "cases": [case.to_wire() for case in self.cases],
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self._wire_without_fingerprint())

    def to_wire(self) -> dict[str, object]:
        return {**self._wire_without_fingerprint(), "corpus_fingerprint": self.fingerprint}


def _derive_corpus(
    corpus_id: str,
    corpus_version: str,
    sources: tuple[PerturbationSource, ...],
    specs: tuple[PerturbationSpec, ...],
    cases: tuple[PerturbationCase, ...],
) -> PerturbationCorpus:
    corpus = object.__new__(PerturbationCorpus)
    object.__setattr__(corpus, "corpus_id", corpus_id)
    object.__setattr__(corpus, "corpus_version", corpus_version)
    object.__setattr__(corpus, "sources", sources)
    object.__setattr__(corpus, "specs", specs)
    object.__setattr__(corpus, "cases", cases)
    corpus.__post_init__()
    return corpus


def build_perturbation_corpus(
    corpus_id: str,
    corpus_version: str,
    sources: tuple[PerturbationSource, ...],
    specs: tuple[PerturbationSpec, ...],
) -> PerturbationCorpus:
    """Build the exact v1 corpus and derive every case from immutable source/spec pairs."""

    _identifier(corpus_id, "corpus_id")
    if corpus_version != PERTURBATION_CORPUS_VERSION:
        raise PerturbationError("unsupported perturbation corpus version")
    if (
        not isinstance(sources, tuple)
        or len(sources) != FROZEN_SOURCE_CLUSTER_COUNT
        or not all(isinstance(source, PerturbationSource) for source in sources)
    ):
        raise PerturbationError("v1 corpus requires exactly 9 typed sources")
    if tuple(source.source_id for source in sources) != tuple(
        sorted(source.source_id for source in sources)
    ):
        raise PerturbationError("sources must use deterministic source_id ordering")
    source_ids = tuple(source.source_id for source in sources)
    cluster_ids = tuple(source.source_cluster_id for source in sources)
    if len(set(source_ids)) != len(source_ids) or len(set(cluster_ids)) != len(cluster_ids):
        raise PerturbationError("source and source-cluster IDs must be unique")
    if (
        not isinstance(specs, tuple)
        or len(specs) != FROZEN_CASE_COUNT
        or not all(isinstance(spec, PerturbationSpec) for spec in specs)
    ):
        raise PerturbationError("v1 corpus requires exactly 27 typed specs")
    if tuple(spec.case_id for spec in specs) != tuple(sorted(spec.case_id for spec in specs)):
        raise PerturbationError("specs must use deterministic case_id ordering")
    case_ids = tuple(spec.case_id for spec in specs)
    if len(set(case_ids)) != len(case_ids):
        raise PerturbationError("case IDs must be unique")
    if {spec.dimension for spec in specs} != set(PerturbationDimension) or len(
        {spec.dimension for spec in specs}
    ) != FROZEN_CASE_COUNT:
        raise PerturbationError("v1 corpus must cover every dimension exactly once")
    source_map = {source.source_id: source for source in sources}
    if any(spec.parent_id not in source_map for spec in specs):
        raise PerturbationError("spec references an unknown parent")
    if {spec.source_cluster_id for spec in specs} != set(cluster_ids):
        raise PerturbationError("every frozen source cluster must be represented")
    cases = tuple(apply_perturbation(source_map[spec.parent_id], spec) for spec in specs)
    return _derive_corpus(corpus_id, corpus_version, sources, specs, cases)


def _keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise PerturbationError(f"{field} members are not closed")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PerturbationError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise PerturbationError(f"{field} must be an array")
    return value


def _parse_source(value: object) -> PerturbationSource:
    document = _object(value, "source")
    _keys(
        document,
        {"source_id", "source_cluster_id", "partition", "material", "source_fingerprint"},
        "source",
    )
    try:
        partition = CorpusPartition(document["partition"])
    except (TypeError, ValueError) as exc:
        raise PerturbationError("source partition is invalid") from exc
    source = PerturbationSource(
        cast(str, document["source_id"]),
        cast(str, document["source_cluster_id"]),
        partition,
        cast(Mapping[str, object], document["material"]),
    )
    if document["source_fingerprint"] != source.fingerprint:
        raise PerturbationError("source fingerprint is not material-derived")
    return source


def _parse_relation(value: object) -> ExpectedRelation:
    document = _object(value, "expected_local_relation")
    _keys(document, {"state", "kind", "direction", "rationale_code"}, "expected relation")
    if document["state"] != "EXPECTED":
        raise PerturbationError("local relation state must be EXPECTED")
    try:
        kind = ExpectedRelationKind(document["kind"])
    except (TypeError, ValueError) as exc:
        raise PerturbationError("expected relation kind is invalid") from exc
    return ExpectedRelation(
        kind,
        cast(str | None, document["direction"]),
        cast(str, document["rationale_code"]),
    )


def _parse_spec(value: object) -> PerturbationSpec:
    document = _object(value, "spec")
    _keys(
        document,
        {
            "case_id",
            "parent_id",
            "source_cluster_id",
            "seed",
            "dimension",
            "operation",
            "target_path",
            "parameters",
            "expected_local_relation",
        },
        "spec",
    )
    parameters = _object(document["parameters"], "spec parameters")
    _keys(parameters, {"value", "indices", "delta", "delete_count"}, "spec parameters")
    try:
        dimension = PerturbationDimension(document["dimension"])
        operation = PerturbationOperation(document["operation"])
    except (TypeError, ValueError) as exc:
        raise PerturbationError("spec enum is invalid") from exc
    indices_value = _list(parameters["indices"], "spec indices")
    return PerturbationSpec(
        cast(str, document["case_id"]),
        cast(str, document["parent_id"]),
        cast(str, document["source_cluster_id"]),
        cast(int, document["seed"]),
        dimension,
        operation,
        cast(str, document["target_path"]),
        parameters["value"],
        tuple(cast(int, item) for item in indices_value),
        cast(int | None, parameters["delta"]),
        cast(int | None, parameters["delete_count"]),
        _parse_relation(document["expected_local_relation"]),
    )


def validate_perturbation_corpus_wire(value: object) -> PerturbationCorpus:
    """Regenerate a portable corpus and reject any caller-declared derived drift."""

    document = _object(value, "perturbation corpus")
    _keys(
        document,
        {
            "schema",
            "corpus_id",
            "corpus_version",
            "source_cluster_count",
            "case_count",
            "dimensions",
            "official_relation_state",
            "local_official_agreement_state",
            "sources",
            "specs",
            "cases",
            "corpus_fingerprint",
        },
        "perturbation corpus",
    )
    if document["schema"] != PERTURBATION_EVALUATION_SCHEMA:
        raise PerturbationError("unsupported perturbation evaluation schema")
    sources = tuple(_parse_source(item) for item in _list(document["sources"], "sources"))
    specs = tuple(_parse_spec(item) for item in _list(document["specs"], "specs"))
    corpus = build_perturbation_corpus(
        cast(str, document["corpus_id"]),
        cast(str, document["corpus_version"]),
        sources,
        specs,
    )
    if corpus.fingerprint != FROZEN_CORPUS_FINGERPRINT:
        raise PerturbationError("perturbation corpus differs from the frozen v1 program")
    if corpus.to_wire() != dict(document):
        raise PerturbationError("perturbation corpus wire is not evaluator-derived")
    return corpus


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PerturbationError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def decode_perturbation_corpus_json(text: str) -> PerturbationCorpus:
    """Decode strict JSON with recursive duplicate-member rejection and semantic regeneration."""

    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MATERIAL_BYTES * 8:
        raise PerturbationError("perturbation corpus JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _v: (_ for _ in ()).throw(
                PerturbationError("JSON number must be finite")
            ),
        )
    except PerturbationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PerturbationError("perturbation corpus must be strict UTF-8 JSON") from exc
    return validate_perturbation_corpus_wire(value)


__all__ = [
    "FROZEN_CASE_COUNT",
    "FROZEN_CORPUS_FINGERPRINT",
    "FROZEN_SOURCE_CLUSTER_COUNT",
    "MAX_MATERIAL_BYTES",
    "MAX_MATERIAL_DEPTH",
    "PERTURBATION_CORPUS_VERSION",
    "PERTURBATION_EVALUATION_SCHEMA",
    "CorpusPartition",
    "ExpectedRelation",
    "ExpectedRelationKind",
    "MissingRelation",
    "MissingRelationState",
    "PerturbationCase",
    "PerturbationCorpus",
    "PerturbationDimension",
    "PerturbationError",
    "PerturbationOperation",
    "PerturbationSource",
    "PerturbationSpec",
    "apply_perturbation",
    "build_perturbation_corpus",
    "decode_perturbation_corpus_json",
    "validate_perturbation_corpus_wire",
]
