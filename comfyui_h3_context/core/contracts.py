"""Versioned, provider-free H3 context vocabulary and contract envelopes.

This module is deliberately limited to immutable typed values. It does not normalize user
requests, inspect media, call providers, or import ComfyUI/model dependencies. Later roadmap items
may add richer contracts, but they must preserve this explicit wire vocabulary and version boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .constraints import HardConstraintSet
from .errors import ContractValidationError

if TYPE_CHECKING:
    from .evidence import EvidenceSet
    from .registry import ReferenceRegistry

_VERSION_PATTERN = re.compile(r"([1-9][0-9]{0,2})\.([0-9]{1,2})\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_MAX_MESSAGE_LENGTH = 4096
_MAX_LOCATION_LENGTH = 256


def _empty_reference_registry() -> ReferenceRegistry:
    """Import the registry lazily to keep the vocabulary module cycle-free."""

    from .registry import ReferenceRegistry

    return ReferenceRegistry.empty()


def _empty_evidence_set() -> EvidenceSet:
    """Import evidence lazily to keep the vocabulary module cycle-free."""

    from .evidence import EvidenceSet

    return EvidenceSet.empty()


@dataclass(frozen=True, slots=True, order=True)
class SchemaVersion:
    """Bounded major/minor schema version represented on the wire as ``major.minor``."""

    major: int
    minor: int

    def __post_init__(self) -> None:
        if isinstance(self.major, bool) or not isinstance(self.major, int):
            raise ContractValidationError("schema major version must be an integer")
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise ContractValidationError("schema minor version must be an integer")
        if not 1 <= self.major <= 999:
            raise ContractValidationError("schema major version must be between 1 and 999")
        if not 0 <= self.minor <= 99:
            raise ContractValidationError("schema minor version must be between 0 and 99")

    @classmethod
    def from_wire(cls, value: object) -> SchemaVersion:
        """Parse a strict semantic version without accepting a leading ``v`` or whitespace."""

        if not isinstance(value, str):
            raise ContractValidationError("schema version must be a string")
        match = _VERSION_PATTERN.fullmatch(value)
        if match is None:
            raise ContractValidationError("schema version must use the form major.minor")
        return cls(int(match.group(1)), int(match.group(2)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


ContractVersion = SchemaVersion
CURRENT_SCHEMA_VERSION = SchemaVersion(1, 0)
CURRENT_PROFILE_VERSION = SchemaVersion(1, 0)


class TaskMode(str, Enum):
    """Closed H3 task families from the pinned Base and Full-Reference guides."""

    T2VA = "t2va"
    I2VA = "i2va"
    FL2VA = "fl2va"
    L2VA = "l2va"
    REF2VA = "ref2va"


class ModelVariant(str, Enum):
    """Explicit open-weight H3-Base conditioning variants."""

    BASE_FL2VA = "base_fl2va"
    BASE_REF2VA = "base_ref2va"


class EvidenceLevel(str, Enum):
    """Confidence/provenance class for a contract rule or observed value."""

    OFFICIAL = "official"
    FRAMEWORK_REFERENCE = "framework_reference"
    COMMUNITY_RECOMMENDED = "community_recommended"
    EXPERIMENTAL = "experimental"
    MODIFIED = "modified"


class AssetRole(str, Enum):
    """Stable source/conditioning roles; detailed reference semantics are later contracts."""

    PRIMARY = "primary"
    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    REFERENCE = "reference"
    SUBJECT_REFERENCE = "subject_reference"
    STYLE_REFERENCE = "style_reference"
    MOTION_REFERENCE = "motion_reference"
    CAMERA_REFERENCE = "camera_reference"
    EDITING_SOURCE = "editing_source"
    CONTINUATION_SOURCE = "continuation_source"
    AUDIO_SOURCE = "audio_source"


class MediaKind(str, Enum):
    """Media kinds accepted by the context pipeline at the contract boundary."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class ProviderIdentity(str, Enum):
    """Explicit provider choices; hosted providers are never implicit fallbacks."""

    MANUAL = "manual"
    LOCAL = "local"
    REMOTE_CUSTOM = "remote_custom"
    OFFICIAL_MINIMAX = "official_minimax"


class ValidationSeverity(str, Enum):
    """Machine-readable diagnostic severity, from informational to fail-closed fatal."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class PromptProfile(str, Enum):
    """Local prompt-profile families represented by the initial schema."""

    BASE = "h3_base"
    FULL_REFERENCE = "h3_full_reference"


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractValidationError(f"{field} must be a {expected.__name__}")


def _require_bounded_identifier(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be a bounded identifier")
    return value


def _require_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractValidationError(f"{field} must be a non-empty bounded string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ContractValidationError(f"{field} must not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    """A closed local profile name paired with its supported profile version."""

    name: PromptProfile
    version: SchemaVersion

    def __post_init__(self) -> None:
        _require_enum(self.name, PromptProfile, "profile name")
        if not isinstance(self.version, SchemaVersion):
            raise ContractValidationError("profile version must be a SchemaVersion")
        if self.version != CURRENT_PROFILE_VERSION:
            raise ContractValidationError(
                f"unsupported profile version {self.version}; current version is "
                f"{CURRENT_PROFILE_VERSION}"
            )

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name.value, "version": str(self.version)}


@dataclass(frozen=True, slots=True)
class AssetDescriptor:
    """Minimal typed asset identity; numbering and backend labels belong to M1-04."""

    asset_id: str
    kind: MediaKind
    role: AssetRole

    def __post_init__(self) -> None:
        _require_bounded_identifier(self.asset_id, "asset_id", _IDENTIFIER_PATTERN)
        _require_enum(self.kind, MediaKind, "media kind")
        _require_enum(self.role, AssetRole, "asset role")

    def to_wire(self) -> dict[str, str]:
        return {"asset_id": self.asset_id, "kind": self.kind.value, "role": self.role.value}


@dataclass(frozen=True, slots=True)
class ValidationDiagnostic:
    """Bounded typed diagnostic; validation codes and locations are defined by later validators."""

    severity: ValidationSeverity
    code: str
    message: str
    location: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.severity, ValidationSeverity, "validation severity")
        _require_bounded_identifier(self.code, "diagnostic code", _CODE_PATTERN)
        _require_text(self.message, "diagnostic message", _MAX_MESSAGE_LENGTH)
        if self.location is not None:
            _require_text(self.location, "diagnostic location", _MAX_LOCATION_LENGTH)

    def to_wire(self) -> dict[str, str | None]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class H3ContextContract:
    """Versioned header plus typed assets, hard constraints, and diagnostics."""

    schema_version: SchemaVersion
    profile: ProfileIdentity
    task_mode: TaskMode
    model_variant: ModelVariant
    evidence_level: EvidenceLevel
    provider: ProviderIdentity
    assets: tuple[AssetDescriptor, ...] = ()
    diagnostics: tuple[ValidationDiagnostic, ...] = ()
    hard_constraints: HardConstraintSet = HardConstraintSet()
    reference_registry: ReferenceRegistry = field(default_factory=_empty_reference_registry)
    evidence: EvidenceSet = field(default_factory=_empty_evidence_set)

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported schema version {self.schema_version}; current version is "
                f"{CURRENT_SCHEMA_VERSION}"
            )
        if not isinstance(self.profile, ProfileIdentity):
            raise ContractValidationError("profile must be a ProfileIdentity")
        _require_enum(self.task_mode, TaskMode, "task mode")
        _require_enum(self.model_variant, ModelVariant, "model variant")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence level")
        _require_enum(self.provider, ProviderIdentity, "provider identity")
        if not isinstance(self.assets, tuple) or not all(
            isinstance(asset, AssetDescriptor) for asset in self.assets
        ):
            raise ContractValidationError("assets must be a tuple of AssetDescriptor values")
        if not isinstance(self.hard_constraints, HardConstraintSet):
            raise ContractValidationError("hard_constraints must be a HardConstraintSet")
        from .registry import ReferenceRegistry

        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise ContractValidationError("reference_registry must be a ReferenceRegistry")
        if self.reference_registry.assets and tuple(self.assets) != (
            self.reference_registry.to_asset_descriptors()
        ):
            raise ContractValidationError(
                "assets and reference_registry must describe the same ordered assets"
            )
        from .evidence import EvidenceSet

        if not isinstance(self.evidence, EvidenceSet):
            raise ContractValidationError("evidence must be an EvidenceSet")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(diagnostic, ValidationDiagnostic) for diagnostic in self.diagnostics
        ):
            raise ContractValidationError(
                "diagnostics must be a tuple of ValidationDiagnostic values"
            )

        if self.profile.name is PromptProfile.BASE and self.task_mode is TaskMode.REF2VA:
            raise ContractValidationError("the h3_base profile does not support ref2va")
        if (
            self.profile.name is PromptProfile.FULL_REFERENCE
            and self.task_mode is not TaskMode.REF2VA
        ):
            raise ContractValidationError("the h3_full_reference profile only supports ref2va")
        expected_variant = (
            ModelVariant.BASE_REF2VA
            if self.task_mode is TaskMode.REF2VA
            else ModelVariant.BASE_FL2VA
        )
        if self.model_variant is not expected_variant:
            raise ContractValidationError(
                f"task mode {self.task_mode.value} requires model variant {expected_variant.value}"
            )

    def to_wire(self) -> dict[str, object]:
        """Return a JSON-compatible representation without exposing mutable contract state."""

        return {
            "schema_version": str(self.schema_version),
            "profile": self.profile.to_wire(),
            "task_mode": self.task_mode.value,
            "model_variant": self.model_variant.value,
            "evidence_level": self.evidence_level.value,
            "provider": self.provider.value,
            "assets": [asset.to_wire() for asset in self.assets],
            "hard_constraints": self.hard_constraints.to_wire(),
            "reference_registry": self.reference_registry.to_wire(),
            "evidence": self.evidence.to_wire(),
            "diagnostics": [diagnostic.to_wire() for diagnostic in self.diagnostics],
        }


__all__ = [
    "AssetDescriptor",
    "AssetRole",
    "ContractVersion",
    "CURRENT_PROFILE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "EvidenceLevel",
    "HardConstraintSet",
    "H3ContextContract",
    "MediaKind",
    "ModelVariant",
    "ProfileIdentity",
    "PromptProfile",
    "ProviderIdentity",
    "SchemaVersion",
    "TaskMode",
    "ValidationDiagnostic",
    "ValidationSeverity",
]
