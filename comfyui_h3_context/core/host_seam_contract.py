"""Pure, content-free contract for the ComfyUI host seams this package consumes.

The census and the observed shape fixture are deliberately separate projections of one authority.
The census may name repository-relative source use sites.  The fixture may contain only closed
shape, readiness, cost-bucket and public version facts; it cannot retain an arbitrary host value,
path or URL.  :func:`parse_host_seam_contract` joins both projections by a closed seam ID and
refuses either projection when it is incomplete or inconsistent.

This module imports no ComfyUI, browser, HTTP, filesystem or provider runtime.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from .canonical import canonical_bytes
from .errors import ContractValidationError

HOST_SEAM_CENSUS_SCHEMA = "h3.context.host_seam_census.v1"
HOST_SEAM_FIXTURE_SCHEMA = "h3.context.host_seam_shape_fixture.v1"
HOST_SEAM_PROFILE = "comfyui_host_seams_v1"
MAX_HOST_SEAMS = 64
MAX_SOURCE_PATHS = 64
MAX_BOUND_VALUE = 1_000_000

_CENSUS_KEYS = frozenset({"schema", "profile", "seams"})
_CENSUS_ROW_KEYS = frozenset(
    {
        "id",
        "layer",
        "owner",
        "classification",
        "authority",
        "source_paths",
        "readiness_states",
        "cost_class",
        "bound",
    }
)
_BOUND_KEYS = frozenset({"observed_floor", "multiplier", "derived_ceiling", "absolute_ceiling"})
_FIXTURE_KEYS = frozenset({"schema", "profile", "subject", "observations"})
_SUBJECT_KEYS = frozenset(
    {"comfyui_version", "comfyui_revision", "frontend_version", "fixture_version"}
)
_OBSERVATION_KEYS = frozenset(
    {
        "seam_id",
        "presence",
        "kind",
        "key_shape",
        "element_kind",
        "readiness_state",
        "count_bucket",
        "byte_bucket",
        "latency_bucket",
    }
)
_IDENTITY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_AUTHORITY = re.compile(r"(?:official|observed)\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\Z")
_OWNER = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_EnumT = TypeVar("_EnumT", bound=Enum)


class HostSeamContractError(ContractValidationError):
    """Raised when census or fixture bytes claim an unsafe or inconsistent seam."""


class HostSeamLayer(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"


class HostSeamClassification(str, Enum):
    DOCUMENTED = "documented"
    UNDOCUMENTED_BUT_OBSERVED = "undocumented_but_observed"


class HostSeamReadiness(str, Enum):
    ABSENT = "absent"
    PRESENT_NOT_READY = "present_not_ready"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class HostSeamCostClass(str, Enum):
    CONSTANT = "constant"
    TARGETED = "targeted"
    FULL_COLLECTION = "full_collection"


class HostSeamPresence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"


class HostSeamKind(str, Enum):
    CALLABLE = "callable"
    COLLECTION = "collection"
    EVENT_TARGET = "event_target"
    MAPPING = "mapping"
    MODULE = "module"
    OBJECT = "object"
    ROUTE_REGISTRY = "route_registry"
    TEXT = "text"


class HostSeamKeyShape(str, Enum):
    CLOSED_MEMBERS = "closed_members"
    NODE_TYPE = "node_type"
    NONE = "none"


class HostSeamElementKind(str, Enum):
    CALLABLE = "callable"
    DISPLAY_NAME = "display_name"
    NODE_CLASS = "node_class"
    NODE_DEFINITION_WRAPPER = "node_definition_wrapper"
    NONE = "none"
    OBJECT = "object"
    PATH_STRING = "path_string"
    ROUTE = "route"


class HostSeamCountBucket(str, Enum):
    NONE = "none"
    ONE = "one"
    TENS = "tens"
    THOUSANDS = "thousands"


class HostSeamByteBucket(str, Enum):
    NOT_MEASURED = "not_measured"
    SUB_1KB = "sub_1kb"
    SUB_100KB = "sub_100kb"
    SUB_100MB = "sub_100mb"


class HostSeamLatencyBucket(str, Enum):
    NOT_MEASURED = "not_measured"
    SUB_10MS = "sub_10ms"
    SUB_100MS = "sub_100ms"
    SUB_5S = "sub_5s"


FULL_READINESS_STATES = tuple(HostSeamReadiness)
STEADY_READINESS_STATES = (
    HostSeamReadiness.READY,
    HostSeamReadiness.UNAVAILABLE,
)


def _object(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise HostSeamContractError(f"{field} has an invalid closed shape")
    if not all(isinstance(key, str) for key in value):
        raise HostSeamContractError(f"{field} contains a non-text key")
    return value


def _list(value: object, field: str, *, maximum: int = MAX_HOST_SEAMS) -> list[object]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise HostSeamContractError(f"{field} must be a bounded non-empty list")
    return value


def _member(enum: type[_EnumT], value: object, field: str) -> _EnumT:
    try:
        return enum(value)
    except (TypeError, ValueError) as error:
        raise HostSeamContractError(f"{field} is outside the closed enum") from error


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise HostSeamContractError(f"{field} must be a bounded seam identity")
    return value


def _authority(value: object, field: str) -> str:
    if not isinstance(value, str) or _AUTHORITY.fullmatch(value) is None:
        raise HostSeamContractError(f"{field} must be a closed authority ID")
    return value


def _version(value: object, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise HostSeamContractError(f"{field} must be a public version")
    return value


def _revision(value: object, field: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise HostSeamContractError(f"{field} must be a full public revision")
    return value


def _source_path(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 260 or _OWNER.fullmatch(value) is None:
        raise HostSeamContractError(f"{field} must be a safe repository-relative path")
    if ".." in value.split("/") or not value.endswith((".py", ".ts", ".tsx")):
        raise HostSeamContractError(f"{field} must name a supported source file")
    return value


def _positive_int(value: object, field: str, *, maximum: int = MAX_BOUND_VALUE) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise HostSeamContractError(f"{field} must be a bounded positive integer")
    return value


@dataclass(frozen=True, slots=True)
class HostSeamBound:
    observed_floor: int
    multiplier: int
    derived_ceiling: int
    absolute_ceiling: int

    def __post_init__(self) -> None:
        floor = _positive_int(self.observed_floor, "bound.observed_floor")
        multiplier = _positive_int(self.multiplier, "bound.multiplier", maximum=16)
        absolute = _positive_int(self.absolute_ceiling, "bound.absolute_ceiling")
        if absolute < floor:
            raise HostSeamContractError("bound.absolute_ceiling cannot be below its floor")
        expected = min(floor * multiplier, absolute)
        if self.derived_ceiling != expected:
            raise HostSeamContractError("bound.derived_ceiling does not match the bounded formula")

    def to_wire(self) -> dict[str, int]:
        return {
            "observed_floor": self.observed_floor,
            "multiplier": self.multiplier,
            "derived_ceiling": self.derived_ceiling,
            "absolute_ceiling": self.absolute_ceiling,
        }


@dataclass(frozen=True, slots=True)
class HostSeamCensusRow:
    seam_id: str
    layer: HostSeamLayer
    owner: str
    classification: HostSeamClassification
    authority: str
    source_paths: tuple[str, ...]
    readiness_states: tuple[HostSeamReadiness, ...]
    cost_class: HostSeamCostClass
    bound: HostSeamBound | None

    def __post_init__(self) -> None:
        _identity(self.seam_id, "seam.id")
        if not isinstance(self.layer, HostSeamLayer):
            raise HostSeamContractError("seam.layer is invalid")
        _source_path(self.owner, "seam.owner")
        if not isinstance(self.classification, HostSeamClassification):
            raise HostSeamContractError("seam.classification is invalid")
        if not isinstance(self.authority, str) or _AUTHORITY.fullmatch(self.authority) is None:
            raise HostSeamContractError("seam.authority must be a closed authority ID")
        expected_prefix = (
            "official." if self.classification is HostSeamClassification.DOCUMENTED else "observed."
        )
        if not self.authority.startswith(expected_prefix):
            raise HostSeamContractError("seam classification and authority disagree")
        if (
            not isinstance(self.source_paths, tuple)
            or not self.source_paths
            or len(self.source_paths) > MAX_SOURCE_PATHS
        ):
            raise HostSeamContractError("seam.source_paths must be a bounded non-empty tuple")
        for path in self.source_paths:
            _source_path(path, "seam.source_paths[]")
        if list(self.source_paths) != sorted(set(self.source_paths)):
            raise HostSeamContractError("seam.source_paths must be sorted and unique")
        if self.owner not in self.source_paths:
            raise HostSeamContractError("seam.owner must be one of its source paths")
        if self.readiness_states not in {FULL_READINESS_STATES, STEADY_READINESS_STATES}:
            raise HostSeamContractError("seam.readiness_states must declare a supported transition")
        if not isinstance(self.cost_class, HostSeamCostClass):
            raise HostSeamContractError("seam.cost_class is invalid")
        if self.cost_class is HostSeamCostClass.FULL_COLLECTION:
            if not isinstance(self.bound, HostSeamBound):
                raise HostSeamContractError("full_collection seams require a measured bound")
        elif self.bound is not None:
            raise HostSeamContractError("only full_collection seams may declare a bound")


@dataclass(frozen=True, slots=True)
class HostSeamSubject:
    comfyui_version: str
    comfyui_revision: str
    frontend_version: str
    fixture_version: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.comfyui_version, "subject.comfyui_version"),
            (self.frontend_version, "subject.frontend_version"),
        ):
            if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
                raise HostSeamContractError(f"{field} must be a public version")
        if (
            not isinstance(self.comfyui_revision, str)
            or _REVISION.fullmatch(self.comfyui_revision) is None
        ):
            raise HostSeamContractError("subject.comfyui_revision must be a full public revision")
        _positive_int(self.fixture_version, "subject.fixture_version", maximum=255)


@dataclass(frozen=True, slots=True)
class HostSeamShape:
    seam_id: str
    presence: HostSeamPresence
    kind: HostSeamKind
    key_shape: HostSeamKeyShape
    element_kind: HostSeamElementKind
    readiness_state: HostSeamReadiness
    count_bucket: HostSeamCountBucket
    byte_bucket: HostSeamByteBucket
    latency_bucket: HostSeamLatencyBucket

    def __post_init__(self) -> None:
        _identity(self.seam_id, "observation.seam_id")
        for value, enum, field in (
            (self.presence, HostSeamPresence, "observation.presence"),
            (self.kind, HostSeamKind, "observation.kind"),
            (self.key_shape, HostSeamKeyShape, "observation.key_shape"),
            (self.element_kind, HostSeamElementKind, "observation.element_kind"),
            (self.readiness_state, HostSeamReadiness, "observation.readiness_state"),
            (self.count_bucket, HostSeamCountBucket, "observation.count_bucket"),
            (self.byte_bucket, HostSeamByteBucket, "observation.byte_bucket"),
            (self.latency_bucket, HostSeamLatencyBucket, "observation.latency_bucket"),
        ):
            if not isinstance(value, enum):
                raise HostSeamContractError(f"{field} is invalid")
        if (
            self.presence is HostSeamPresence.ABSENT
            and self.readiness_state is not HostSeamReadiness.ABSENT
        ):
            raise HostSeamContractError("an absent observation must carry absent readiness")
        if (
            self.presence is HostSeamPresence.PRESENT
            and self.readiness_state is HostSeamReadiness.ABSENT
        ):
            raise HostSeamContractError("a present observation cannot carry absent readiness")


@dataclass(frozen=True, slots=True)
class HostSeamRow:
    census: HostSeamCensusRow
    shape: HostSeamShape

    @property
    def seam_id(self) -> str:
        return self.census.seam_id

    @property
    def bound(self) -> HostSeamBound | None:
        return self.census.bound

    def __post_init__(self) -> None:
        if self.census.seam_id != self.shape.seam_id:
            raise HostSeamContractError("census and fixture seam IDs disagree")
        if self.shape.readiness_state not in self.census.readiness_states:
            raise HostSeamContractError("fixture readiness is outside the census transition")


@dataclass(frozen=True, slots=True)
class HostSeamContract:
    profile: str
    subject: HostSeamSubject
    rows: tuple[HostSeamRow, ...]

    def __post_init__(self) -> None:
        if self.profile != HOST_SEAM_PROFILE:
            raise HostSeamContractError("unsupported host seam profile")
        if not self.rows or len(self.rows) > MAX_HOST_SEAMS:
            raise HostSeamContractError("host seam rows must be bounded and non-empty")
        ids = [row.seam_id for row in self.rows]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise HostSeamContractError("host seam rows must be sorted and unique")

    @property
    def fingerprint(self) -> str:
        payload = {
            "profile": self.profile,
            "subject": {
                "comfyui_version": self.subject.comfyui_version,
                "comfyui_revision": self.subject.comfyui_revision,
                "frontend_version": self.subject.frontend_version,
                "fixture_version": self.subject.fixture_version,
            },
            "seams": [row.seam_id for row in self.rows],
        }
        return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _parse_bound(value: object) -> HostSeamBound | None:
    if value is None:
        return None
    raw = _object(value, "bound", _BOUND_KEYS)
    return HostSeamBound(
        observed_floor=_positive_int(raw["observed_floor"], "bound.observed_floor"),
        multiplier=_positive_int(raw["multiplier"], "bound.multiplier", maximum=16),
        derived_ceiling=_positive_int(raw["derived_ceiling"], "bound.derived_ceiling"),
        absolute_ceiling=_positive_int(raw["absolute_ceiling"], "bound.absolute_ceiling"),
    )


def _parse_census(value: object) -> tuple[str, tuple[HostSeamCensusRow, ...]]:
    root = _object(value, "census", _CENSUS_KEYS)
    if root["schema"] != HOST_SEAM_CENSUS_SCHEMA or root["profile"] != HOST_SEAM_PROFILE:
        raise HostSeamContractError("unsupported host seam census schema or profile")
    rows: list[HostSeamCensusRow] = []
    for index, value in enumerate(_list(root["seams"], "census.seams")):
        raw = _object(value, f"census.seams[{index}]", _CENSUS_ROW_KEYS)
        source_paths = tuple(
            _source_path(item, "seam.source_paths[]")
            for item in _list(raw["source_paths"], "seam.source_paths", maximum=MAX_SOURCE_PATHS)
        )
        states = tuple(
            _member(HostSeamReadiness, item, "seam.readiness_states[]")
            for item in _list(raw["readiness_states"], "seam.readiness_states", maximum=4)
        )
        rows.append(
            HostSeamCensusRow(
                seam_id=_identity(raw["id"], "seam.id"),
                layer=_member(HostSeamLayer, raw["layer"], "seam.layer"),
                owner=_source_path(raw["owner"], "seam.owner"),
                classification=_member(
                    HostSeamClassification, raw["classification"], "seam.classification"
                ),
                authority=_authority(raw["authority"], "seam.authority"),
                source_paths=source_paths,
                readiness_states=states,
                cost_class=_member(HostSeamCostClass, raw["cost_class"], "seam.cost_class"),
                bound=_parse_bound(raw["bound"]),
            )
        )
    ids = [row.seam_id for row in rows]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise HostSeamContractError("census seams must be sorted and unique")
    return HOST_SEAM_PROFILE, tuple(rows)


def _parse_fixture(value: object) -> tuple[str, HostSeamSubject, tuple[HostSeamShape, ...]]:
    root = _object(value, "fixture", _FIXTURE_KEYS)
    if root["schema"] != HOST_SEAM_FIXTURE_SCHEMA or root["profile"] != HOST_SEAM_PROFILE:
        raise HostSeamContractError("unsupported host seam fixture schema or profile")
    raw_subject = _object(root["subject"], "fixture.subject", _SUBJECT_KEYS)
    subject = HostSeamSubject(
        comfyui_version=_version(raw_subject["comfyui_version"], "subject.comfyui_version"),
        comfyui_revision=_revision(raw_subject["comfyui_revision"], "subject.comfyui_revision"),
        frontend_version=_version(raw_subject["frontend_version"], "subject.frontend_version"),
        fixture_version=_positive_int(
            raw_subject["fixture_version"], "subject.fixture_version", maximum=255
        ),
    )
    observations: list[HostSeamShape] = []
    for index, value in enumerate(_list(root["observations"], "fixture.observations")):
        raw = _object(value, f"fixture.observations[{index}]", _OBSERVATION_KEYS)
        observations.append(
            HostSeamShape(
                seam_id=_identity(raw["seam_id"], "observation.seam_id"),
                presence=_member(HostSeamPresence, raw["presence"], "observation.presence"),
                kind=_member(HostSeamKind, raw["kind"], "observation.kind"),
                key_shape=_member(HostSeamKeyShape, raw["key_shape"], "observation.key_shape"),
                element_kind=_member(
                    HostSeamElementKind, raw["element_kind"], "observation.element_kind"
                ),
                readiness_state=_member(
                    HostSeamReadiness, raw["readiness_state"], "observation.readiness_state"
                ),
                count_bucket=_member(
                    HostSeamCountBucket, raw["count_bucket"], "observation.count_bucket"
                ),
                byte_bucket=_member(
                    HostSeamByteBucket, raw["byte_bucket"], "observation.byte_bucket"
                ),
                latency_bucket=_member(
                    HostSeamLatencyBucket,
                    raw["latency_bucket"],
                    "observation.latency_bucket",
                ),
            )
        )
    ids = [row.seam_id for row in observations]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise HostSeamContractError("fixture observations must be sorted and unique")
    return HOST_SEAM_PROFILE, subject, tuple(observations)


def parse_host_seam_contract(census: object, fixture: object) -> HostSeamContract:
    """Validate and join the two deterministic host-seam projections."""

    census_profile, census_rows = _parse_census(census)
    fixture_profile, subject, fixture_rows = _parse_fixture(fixture)
    if census_profile != fixture_profile:
        raise HostSeamContractError("census and fixture profiles disagree")
    census_by_id = {row.seam_id: row for row in census_rows}
    fixture_by_id = {row.seam_id: row for row in fixture_rows}
    if set(census_by_id) != set(fixture_by_id):
        raise HostSeamContractError("census and fixture seam IDs do not join exactly")
    return HostSeamContract(
        profile=census_profile,
        subject=subject,
        rows=tuple(
            HostSeamRow(census=census_by_id[seam_id], shape=fixture_by_id[seam_id])
            for seam_id in sorted(census_by_id)
        ),
    )


def classify_mapping_readiness(value: object, *, readiness_reached: bool) -> HostSeamReadiness:
    """Classify a readiness-sensitive mapping without conflating empty and absent.

    ``registered_node_types`` exists as an empty mapping before ComfyUI finishes node registration.
    IMPORTANT: do not replace this with a truthiness check; it would turn not-ready into absent and
    silently select the wrong fallback.
    """

    if value is None:
        return HostSeamReadiness.ABSENT
    if not isinstance(value, Mapping):
        return HostSeamReadiness.UNAVAILABLE
    if not readiness_reached:
        return HostSeamReadiness.PRESENT_NOT_READY
    return HostSeamReadiness.READY if len(value) > 0 else HostSeamReadiness.UNAVAILABLE


__all__ = [
    "FULL_READINESS_STATES",
    "HOST_SEAM_CENSUS_SCHEMA",
    "HOST_SEAM_FIXTURE_SCHEMA",
    "HOST_SEAM_PROFILE",
    "HostSeamBound",
    "HostSeamClassification",
    "HostSeamContract",
    "HostSeamContractError",
    "HostSeamCostClass",
    "HostSeamReadiness",
    "HostSeamRow",
    "HostSeamShape",
    "HostSeamSubject",
    "classify_mapping_readiness",
    "parse_host_seam_contract",
]
