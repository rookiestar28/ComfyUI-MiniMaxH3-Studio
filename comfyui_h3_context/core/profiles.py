"""Immutable versioned prompt-profile declarations for the provider-free compiler.

Profiles describe source-qualified prompt contracts.  They do not render text, inspect media,
select providers, or perform automatic profile inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .contracts import (
    CURRENT_PROFILE_VERSION,
    CURRENT_SCHEMA_VERSION,
    EvidenceLevel,
    ProfileIdentity,
    PromptProfile,
    ProviderIdentity,
    SchemaVersion,
    TaskMode,
    ValidationSeverity,
)
from .errors import ContractValidationError, ProfileRegistryError
from .normalization import (
    MAX_FRAME_COUNT,
    MIN_FRAME_COUNT,
    DurationSource,
)

PROFILE_REGISTRY_SCHEMA = "h3-prompt-profile-registry/1"
MAX_PROFILE_DEFINITIONS = 64
MAX_PROFILE_ITEMS = 64
MAX_PROFILE_TEXT_LENGTH = 4096
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_FIELD_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_LABEL_PATTERN = re.compile(r"<[A-Za-z][A-Za-z ]*\{ordinal\}>\Z")


def _require_identifier(
    value: object, field: str, pattern: re.Pattern[str] = _IDENTIFIER_PATTERN
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ProfileRegistryError(f"{field} must be a bounded identifier")
    return value


def _require_text(value: object, field: str, maximum: int = MAX_PROFILE_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProfileRegistryError(f"{field} must be a non-empty bounded string")
    lowered = value.lower()
    forbidden = ("http://", "https://", "authorization", "bearer ", "api_key", "secret")
    if any(marker in lowered for marker in forbidden) or "/" in value or "\\" in value:
        raise ProfileRegistryError(f"{field} contains unsafe metadata")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ProfileRegistryError(f"{field} contains a control character")
    return value


def _require_enum(value: object, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ProfileRegistryError(f"{field} must be a {expected.__name__}")


def _require_unique(values: tuple[object, ...], field: str) -> None:
    if len(values) != len(set(values)):
        raise ProfileRegistryError(f"{field} must not contain duplicates")


class DurationAlignment(str, Enum):
    """Reference-frame alignment required by a prompt profile."""

    NONE = "none"
    FIRST = "first"
    FIRST_AND_LAST = "first_and_last"
    LAST = "last"


class ReferenceLabelKind(str, Enum):
    """Closed label families used by Base and Full-Reference prompt guides."""

    SUBJECT = "subject"
    PICTURE = "picture"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class ProfileSource:
    """Bounded provenance metadata for one profile definition."""

    source_id: str
    source_revision: str
    evidence_level: EvidenceLevel
    official_override: bool = False
    modified_from: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.source_id, "profile source_id")
        _require_text(self.source_revision, "profile source_revision", 256)
        _require_enum(self.evidence_level, EvidenceLevel, "profile evidence_level")
        if not isinstance(self.official_override, bool):
            raise ProfileRegistryError("profile official_override must be a bool")
        if self.modified_from is not None:
            _require_identifier(self.modified_from, "profile modified_from")
        if self.official_override and self.evidence_level is not EvidenceLevel.MODIFIED:
            raise ProfileRegistryError(
                "an official profile override must use the modified evidence level"
            )
        if self.official_override and self.modified_from is None:
            raise ProfileRegistryError(
                "an official profile override must identify its base profile"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "evidence_level": self.evidence_level.value,
            "official_override": self.official_override,
            "modified_from": self.modified_from,
        }


@dataclass(frozen=True, slots=True)
class DurationRule:
    """Explicit duration input precedence and keyframe alignment metadata."""

    source_precedence: tuple[DurationSource, ...]
    frame_rate: int
    minimum_frame_count: int
    maximum_frame_count: int
    alignment: DurationAlignment
    time_precision: int = 2
    mode_alignments: tuple[tuple[TaskMode, DurationAlignment], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_precedence, tuple) or not self.source_precedence:
            raise ProfileRegistryError("duration source_precedence must be a non-empty tuple")
        if not all(isinstance(value, DurationSource) for value in self.source_precedence):
            raise ProfileRegistryError("duration source_precedence contains an invalid value")
        _require_unique(self.source_precedence, "duration source_precedence")
        if (
            isinstance(self.frame_rate, bool)
            or not isinstance(self.frame_rate, int)
            or not 1 <= self.frame_rate <= 240
        ):
            raise ProfileRegistryError("duration frame_rate must be between 1 and 240")
        if (
            isinstance(self.minimum_frame_count, bool)
            or not isinstance(self.minimum_frame_count, int)
            or not MIN_FRAME_COUNT <= self.minimum_frame_count <= MAX_FRAME_COUNT
        ):
            raise ProfileRegistryError(
                "duration minimum_frame_count is outside the supported range"
            )
        if (
            isinstance(self.maximum_frame_count, bool)
            or not isinstance(self.maximum_frame_count, int)
            or not self.minimum_frame_count <= self.maximum_frame_count <= MAX_FRAME_COUNT
        ):
            raise ProfileRegistryError(
                "duration maximum_frame_count is outside the supported range"
            )
        _require_enum(self.alignment, DurationAlignment, "duration alignment")
        if (
            isinstance(self.time_precision, bool)
            or not isinstance(self.time_precision, int)
            or not 0 <= self.time_precision <= 6
        ):
            raise ProfileRegistryError("duration time_precision must be between 0 and 6")
        if (
            not isinstance(self.mode_alignments, tuple)
            or len(self.mode_alignments) > MAX_PROFILE_ITEMS
        ):
            raise ProfileRegistryError("duration mode_alignments must be a bounded tuple")
        for mode, alignment in self.mode_alignments:
            _require_enum(mode, TaskMode, "duration mode alignment mode")
            _require_enum(alignment, DurationAlignment, "duration mode alignment")
        _require_unique(
            tuple(mode for mode, _ in self.mode_alignments), "duration mode alignment modes"
        )

    def alignment_for(self, mode: TaskMode) -> DurationAlignment:
        """Return an explicit mode override or the profile-wide alignment."""

        if not isinstance(mode, TaskMode):
            raise ProfileRegistryError("duration alignment lookup requires a TaskMode")
        for declared_mode, alignment in self.mode_alignments:
            if declared_mode is mode:
                return alignment
        return self.alignment

    def to_wire(self) -> dict[str, object]:
        return {
            "source_precedence": [value.value for value in self.source_precedence],
            "frame_rate": self.frame_rate,
            "minimum_frame_count": self.minimum_frame_count,
            "maximum_frame_count": self.maximum_frame_count,
            "alignment": self.alignment.value,
            "time_precision": self.time_precision,
            "mode_alignments": [
                {"mode": mode.value, "alignment": alignment.value}
                for mode, alignment in self.mode_alignments
            ],
        }


@dataclass(frozen=True, slots=True)
class LabelRule:
    """One declared reference-label syntax and its stable ordinal scope."""

    kind: ReferenceLabelKind
    syntax: str
    ordinal_scope: str
    description: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, ReferenceLabelKind, "label kind")
        if not isinstance(self.syntax, str) or _LABEL_PATTERN.fullmatch(self.syntax) is None:
            raise ProfileRegistryError("label syntax must contain one {ordinal} placeholder")
        _require_identifier(self.ordinal_scope, "label ordinal_scope")
        _require_text(self.description, "label description")

    def to_wire(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "syntax": self.syntax,
            "ordinal_scope": self.ordinal_scope,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ProfileValidationRule:
    """A deterministic structural/semantic validation rule declared by a profile."""

    code: str
    severity: ValidationSeverity
    description: str
    remediation: str

    def __post_init__(self) -> None:
        _require_identifier(self.code, "profile validation code", _FIELD_PATTERN)
        _require_enum(self.severity, ValidationSeverity, "profile validation severity")
        _require_text(self.description, "profile validation description")
        _require_text(self.remediation, "profile validation remediation")

    def to_wire(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "description": self.description,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class ProfileLimitation:
    """A visible bounded limitation that must accompany a profile."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _require_identifier(self.code, "profile limitation code", _FIELD_PATTERN)
        _require_text(self.message, "profile limitation message")

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class PromptProfileDefinition:
    """Complete source-qualified declaration consumed by later renderers and validators."""

    identity: ProfileIdentity
    source: ProfileSource
    supported_modes: tuple[TaskMode, ...]
    required_fields: tuple[str, ...]
    duration_rule: DurationRule
    label_rules: tuple[LabelRule, ...]
    render_order: tuple[str, ...]
    validation_rules: tuple[ProfileValidationRule, ...]
    provider_compatibility: tuple[ProviderIdentity, ...]
    limitations: tuple[ProfileLimitation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProfileIdentity):
            raise ProfileRegistryError("profile identity must be a ProfileIdentity")
        if self.identity.version != CURRENT_PROFILE_VERSION:
            raise ProfileRegistryError("profile definition uses an unsupported profile version")
        if not isinstance(self.source, ProfileSource):
            raise ProfileRegistryError("profile source must be a ProfileSource")
        if not isinstance(self.supported_modes, tuple) or not self.supported_modes:
            raise ProfileRegistryError("supported_modes must be a non-empty tuple")
        if not all(isinstance(value, TaskMode) for value in self.supported_modes):
            raise ProfileRegistryError("supported_modes contains an invalid task mode")
        _require_unique(self.supported_modes, "supported_modes")
        if not isinstance(self.required_fields, tuple) or not self.required_fields:
            raise ProfileRegistryError("required_fields must be a non-empty tuple")
        for field in self.required_fields:
            _require_identifier(field, "profile required field", _FIELD_PATTERN)
        _require_unique(self.required_fields, "required_fields")
        if not isinstance(self.duration_rule, DurationRule):
            raise ProfileRegistryError("duration_rule must be a DurationRule")
        if not isinstance(self.label_rules, tuple) or len(self.label_rules) > MAX_PROFILE_ITEMS:
            raise ProfileRegistryError("label_rules must be a bounded tuple")
        if not all(isinstance(value, LabelRule) for value in self.label_rules):
            raise ProfileRegistryError("label_rules contains an invalid value")
        _require_unique(tuple(value.kind for value in self.label_rules), "label rule kinds")
        if not isinstance(self.render_order, tuple) or not self.render_order:
            raise ProfileRegistryError("render_order must be a non-empty tuple")
        for field in self.render_order:
            _require_identifier(field, "profile render field", _FIELD_PATTERN)
        _require_unique(self.render_order, "render_order")
        if set(self.required_fields) != set(self.render_order):
            raise ProfileRegistryError("render_order must contain exactly the required fields")
        if not isinstance(self.validation_rules, tuple) or not self.validation_rules:
            raise ProfileRegistryError("validation_rules must be a non-empty tuple")
        if not all(isinstance(value, ProfileValidationRule) for value in self.validation_rules):
            raise ProfileRegistryError("validation_rules contains an invalid value")
        _require_unique(
            tuple(value.code for value in self.validation_rules), "validation rule codes"
        )
        if not isinstance(self.provider_compatibility, tuple) or not self.provider_compatibility:
            raise ProfileRegistryError("provider_compatibility must be a non-empty tuple")
        if not all(isinstance(value, ProviderIdentity) for value in self.provider_compatibility):
            raise ProfileRegistryError("provider_compatibility contains an invalid value")
        _require_unique(self.provider_compatibility, "provider_compatibility")
        if not isinstance(self.limitations, tuple) or len(self.limitations) > MAX_PROFILE_ITEMS:
            raise ProfileRegistryError("limitations must be a bounded tuple")
        if not all(isinstance(value, ProfileLimitation) for value in self.limitations):
            raise ProfileRegistryError("limitations contains an invalid value")
        _require_unique(tuple(value.code for value in self.limitations), "profile limitation codes")
        if self.identity.name is PromptProfile.BASE and TaskMode.REF2VA in self.supported_modes:
            raise ProfileRegistryError("h3_base cannot declare ref2va support")
        if self.identity.name is PromptProfile.FULL_REFERENCE and self.supported_modes != (
            TaskMode.REF2VA,
        ):
            raise ProfileRegistryError("h3_full_reference must declare only ref2va support")

    def supports(self, mode: TaskMode) -> bool:
        """Return whether this explicit profile supports one task mode."""

        return mode in self.supported_modes

    def to_wire(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_wire(),
            "source": self.source.to_wire(),
            "supported_modes": [value.value for value in self.supported_modes],
            "required_fields": list(self.required_fields),
            "duration_rule": self.duration_rule.to_wire(),
            "label_rules": [value.to_wire() for value in self.label_rules],
            "render_order": list(self.render_order),
            "validation_rules": [value.to_wire() for value in self.validation_rules],
            "provider_compatibility": [value.value for value in self.provider_compatibility],
            "limitations": [value.to_wire() for value in self.limitations],
        }


@dataclass(frozen=True, slots=True)
class PromptProfileRegistry:
    """Immutable, explicitly resolved profile definitions."""

    definitions: tuple[PromptProfileDefinition, ...]
    schema_version: SchemaVersion = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ProfileRegistryError("unsupported prompt-profile registry schema version")
        if (
            not isinstance(self.definitions, tuple)
            or len(self.definitions) > MAX_PROFILE_DEFINITIONS
        ):
            raise ProfileRegistryError("profile definitions exceed the registry limit")
        if not all(isinstance(value, PromptProfileDefinition) for value in self.definitions):
            raise ProfileRegistryError("definitions contains an invalid profile")
        identities = tuple(
            (value.identity.name, value.identity.version) for value in self.definitions
        )
        _require_unique(identities, "profile identities")

    def get(self, identity: ProfileIdentity) -> PromptProfileDefinition:
        """Resolve an already typed identity; unknown values fail without fallback."""

        if not isinstance(identity, ProfileIdentity):
            raise ProfileRegistryError("profile lookup requires a ProfileIdentity")
        for definition in self.definitions:
            if definition.identity == identity:
                return definition
        raise ProfileRegistryError(
            f"unknown prompt profile {identity.name.value}/{identity.version}"
        )

    def resolve(
        self, name: PromptProfile | str, version: SchemaVersion | str
    ) -> PromptProfileDefinition:
        """Resolve wire-like name/version values with explicit unknown-value errors."""

        try:
            profile_name = name if isinstance(name, PromptProfile) else PromptProfile(name)
        except (TypeError, ValueError) as exc:
            raise ProfileRegistryError(f"unknown prompt profile name: {name!r}") from exc
        try:
            profile_version = (
                version if isinstance(version, SchemaVersion) else SchemaVersion.from_wire(version)
            )
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise ProfileRegistryError(f"invalid prompt profile version: {version!r}") from exc
        if profile_version != CURRENT_PROFILE_VERSION:
            raise ProfileRegistryError(f"unknown prompt profile version: {profile_version}")
        return self.get(ProfileIdentity(profile_name, profile_version))

    def for_mode(self, mode: TaskMode) -> tuple[PromptProfileDefinition, ...]:
        """Return matching profiles without selecting one implicitly."""

        if not isinstance(mode, TaskMode):
            raise ProfileRegistryError("profile mode lookup requires a TaskMode")
        return tuple(definition for definition in self.definitions if definition.supports(mode))

    def with_definition(self, definition: PromptProfileDefinition) -> PromptProfileRegistry:
        """Return a new registry after explicit duplicate-checked registration."""

        if not isinstance(definition, PromptProfileDefinition):
            raise ProfileRegistryError("registered value must be a PromptProfileDefinition")
        if any(item.identity == definition.identity for item in self.definitions):
            raise ProfileRegistryError(f"duplicate prompt profile identity: {definition.identity}")
        return PromptProfileRegistry(self.definitions + (definition,), self.schema_version)

    def to_wire(self) -> dict[str, object]:
        return {
            "registry_schema": PROFILE_REGISTRY_SCHEMA,
            "schema_version": str(self.schema_version),
            "profiles": [value.to_wire() for value in self.definitions],
        }


def _base_profile() -> PromptProfileDefinition:
    return PromptProfileDefinition(
        identity=ProfileIdentity(PromptProfile.BASE, CURRENT_PROFILE_VERSION),
        source=ProfileSource(
            "video_prompt_guide.base",
            "local-guide-2026-07-30",
            EvidenceLevel.FRAMEWORK_REFERENCE,
        ),
        supported_modes=(TaskMode.T2VA, TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA),
        required_fields=(
            "integrated_multimodal_description",
            "overall_soundscape",
            "non_diegetic_music",
        ),
        duration_rule=DurationRule(
            (DurationSource.SECONDS, DurationSource.DEFAULT),
            24,
            MIN_FRAME_COUNT,
            MAX_FRAME_COUNT,
            DurationAlignment.NONE,
            mode_alignments=(
                (TaskMode.T2VA, DurationAlignment.NONE),
                (TaskMode.I2VA, DurationAlignment.FIRST),
                (TaskMode.FL2VA, DurationAlignment.FIRST_AND_LAST),
                (TaskMode.L2VA, DurationAlignment.LAST),
            ),
        ),
        label_rules=(
            LabelRule(
                ReferenceLabelKind.PICTURE,
                "<Picture {ordinal}>",
                "picture_reference_order",
                "Keyframe and image references use independent one-based picture ordinals.",
            ),
        ),
        render_order=(
            "integrated_multimodal_description",
            "overall_soundscape",
            "non_diegetic_music",
        ),
        validation_rules=(
            ProfileValidationRule(
                "base.required_sections",
                ValidationSeverity.ERROR,
                "All three Base guide sections must be present in declared order.",
                "Render the missing section or report a validation error.",
            ),
            ProfileValidationRule(
                "base.exact_text",
                ValidationSeverity.ERROR,
                "Declared dialogue and visible text must remain verbatim.",
                "Compare against the immutable hard-constraint set.",
            ),
            ProfileValidationRule(
                "base.duration_alignment",
                ValidationSeverity.ERROR,
                "Keyframe alignment must use the normalized effective duration.",
                "Use the request effective duration and the profile precision.",
            ),
        ),
        provider_compatibility=tuple(ProviderIdentity),
        limitations=(
            ProfileLimitation(
                "base.no_media_inference",
                "The deterministic profile does not infer observations from media.",
            ),
        ),
    )


def _full_reference_profile() -> PromptProfileDefinition:
    return PromptProfileDefinition(
        identity=ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_PROFILE_VERSION),
        source=ProfileSource(
            "video_prompt_guide.full_reference",
            "local-guide-2026-07-30",
            EvidenceLevel.FRAMEWORK_REFERENCE,
        ),
        supported_modes=(TaskMode.REF2VA,),
        required_fields=(
            "subject_definitions",
            "summary",
            "retention_analysis",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        ),
        duration_rule=DurationRule(
            (DurationSource.SECONDS, DurationSource.DEFAULT),
            24,
            MIN_FRAME_COUNT,
            MAX_FRAME_COUNT,
            DurationAlignment.NONE,
            mode_alignments=((TaskMode.REF2VA, DurationAlignment.NONE),),
        ),
        label_rules=tuple(
            LabelRule(
                kind, f"<{kind.value.title()} {{ordinal}}>", "reference_category_order", description
            )
            for kind, description in (
                (ReferenceLabelKind.SUBJECT, "Reusable visible content labels."),
                (ReferenceLabelKind.PICTURE, "Concrete image and keyframe reference labels."),
                (ReferenceLabelKind.VIDEO, "Whole-video source and structure labels."),
                (ReferenceLabelKind.AUDIO, "Standalone audio source labels."),
            )
        ),
        render_order=(
            "subject_definitions",
            "summary",
            "retention_analysis",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        ),
        validation_rules=(
            ProfileValidationRule(
                "full_reference.required_sections",
                ValidationSeverity.ERROR,
                "All six Full-Reference guide sections must be present in declared order.",
                "Render the missing section or report a validation error.",
            ),
            ProfileValidationRule(
                "full_reference.label_ownership",
                ValidationSeverity.ERROR,
                "Reference labels retain one meaning across every section.",
                "Resolve labels from the explicit reference registry before rendering.",
            ),
            ProfileValidationRule(
                "full_reference.retention_marker",
                ValidationSeverity.ERROR,
                "Retention markers must match the declared visual or audio role.",
                "Report an invalid marker instead of silently changing it.",
            ),
        ),
        provider_compatibility=tuple(ProviderIdentity),
        limitations=(
            ProfileLimitation(
                "full_reference.explicit_evidence",
                "Reference observations and relationships require explicit evidence records.",
            ),
            ProfileLimitation(
                "full_reference.no_private_equivalence",
                "The local guide profile is not a claim of private Context-IR equivalence.",
            ),
        ),
    )


def default_profile_registry() -> PromptProfileRegistry:
    """Return a fresh immutable registry containing the accepted built-in profiles."""

    return PromptProfileRegistry((_base_profile(), _full_reference_profile()))


__all__ = [
    "DurationAlignment",
    "DurationRule",
    "LabelRule",
    "MAX_PROFILE_DEFINITIONS",
    "MAX_PROFILE_ITEMS",
    "MAX_PROFILE_TEXT_LENGTH",
    "PROFILE_REGISTRY_SCHEMA",
    "ProfileLimitation",
    "ProfileSource",
    "ProfileValidationRule",
    "PromptProfileDefinition",
    "PromptProfileRegistry",
    "ReferenceLabelKind",
    "default_profile_registry",
]
