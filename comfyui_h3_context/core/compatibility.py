"""Provider-free compatibility and co-installation contracts for M7-06.

The pure-core side only compares bounded, caller-supplied facts. It never imports ComfyUI, Torch,
media libraries, HTTP clients, or model runtimes and never attempts to repair an incompatible host.
Host discovery belongs to the read-only M7-06 script and the existing supported-host driver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical import canonical_fingerprint
from .errors import CompatibilityError

COMPATIBILITY_MATRIX_SCHEMA = "h3.compatibility.matrix.v1"
MAX_COMPATIBILITY_PROFILES = 32
MAX_COMPATIBILITY_DEPENDENCIES = 64
MAX_COMPATIBILITY_NATIVE_NODES = 64
MAX_COMPATIBILITY_OPTIONAL_ROOTS = 64
MAX_COMPATIBILITY_DIAGNOSTICS = 64
MAX_COMPATIBILITY_MESSAGE_LENGTH = 512

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "cookie",
    "credential",
    "password",
    "private",
    "secret",
    "token=",
    "sig=",
    "/mnt/",
    "\\\\",
)


class CompatibilityStatus(str, Enum):
    """Terminal classification of a named matrix profile."""

    SUPPORTED = "supported"
    VALIDATED_DRIFTED = "validated_drifted"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class PlatformFamily(str, Enum):
    """Closed platform vocabulary used by the compatibility matrix."""

    ANY = "any"
    WINDOWS = "windows"
    LINUX = "linux"
    LINUX_WSL = "linux_wsl"
    MACOS = "macos"


@dataclass(frozen=True, slots=True, order=True)
class CompatibilityVersion:
    """Strict comparable major/minor/patch version used for runtime facts."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.major, "version major"),
            (self.minor, "version minor"),
            (self.patch, "version patch"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
                raise CompatibilityError("invalid_version", f"{field} must be between 0 and 999")

    @classmethod
    def from_wire(cls, value: object) -> CompatibilityVersion:
        if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
            raise CompatibilityError("invalid_version", "version must use major.minor.patch")
        return cls(*(int(part) for part in value.split(".")))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _bounded_identifier(value: object, field: str, *, package: bool = False) -> str:
    pattern = _PACKAGE_PATTERN if package else _IDENTIFIER_PATTERN
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CompatibilityError("invalid_identifier", f"{field} must be a bounded identifier")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise CompatibilityError("sensitive_metadata", f"{field} contains sensitive metadata")
    return value


def _bounded_message(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_COMPATIBILITY_MESSAGE_LENGTH:
        raise CompatibilityError("invalid_message", f"{field} must be bounded and non-empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise CompatibilityError("invalid_message", f"{field} contains a control character")
    if any(marker in value.casefold() for marker in _SENSITIVE_MARKERS):
        raise CompatibilityError("sensitive_metadata", f"{field} contains sensitive metadata")
    return value


def _tuple_unique(values: tuple[str, ...], field: str, maximum: int) -> None:
    if len(values) > maximum:
        raise CompatibilityError("limit_exceeded", f"{field} exceeds its bounded limit")
    if len(values) != len(set(values)):
        raise CompatibilityError("duplicate_value", f"{field} must not contain duplicates")


def _revision_parts(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [value[index : index + 5] for index in range(0, len(value), 5)]


@dataclass(frozen=True, slots=True)
class DependencyConstraint:
    """One required or optional package version range without a resolver side effect."""

    name: str
    minimum: CompatibilityVersion | None = None
    maximum: CompatibilityVersion | None = None
    exact: CompatibilityVersion | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        _bounded_identifier(self.name, "dependency name", package=True)
        if self.minimum is not None and not isinstance(self.minimum, CompatibilityVersion):
            raise CompatibilityError("invalid_dependency", "dependency minimum is invalid")
        if self.maximum is not None and not isinstance(self.maximum, CompatibilityVersion):
            raise CompatibilityError("invalid_dependency", "dependency maximum is invalid")
        if self.exact is not None and not isinstance(self.exact, CompatibilityVersion):
            raise CompatibilityError("invalid_dependency", "dependency exact version is invalid")
        if self.exact is not None and (self.minimum is not None or self.maximum is not None):
            raise CompatibilityError("invalid_dependency", "exact cannot be combined with a range")
        if self.minimum is None and self.maximum is None and self.exact is None:
            raise CompatibilityError("invalid_dependency", "dependency needs a version constraint")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise CompatibilityError("invalid_dependency", "dependency range is reversed")
        if not isinstance(self.optional, bool):
            raise CompatibilityError("invalid_dependency", "dependency optional flag is invalid")

    def accepts(self, version: CompatibilityVersion) -> bool:
        if not isinstance(version, CompatibilityVersion):
            raise CompatibilityError("invalid_version", "observed dependency version is invalid")
        if self.exact is not None:
            return version == self.exact
        if self.minimum is not None and version < self.minimum:
            return False
        if self.maximum is not None and version > self.maximum:
            return False
        return True

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "minimum": None if self.minimum is None else str(self.minimum),
            "maximum": None if self.maximum is None else str(self.maximum),
            "exact": None if self.exact is None else str(self.exact),
            "optional": self.optional,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityProfile:
    """Declarative requirements for one named runtime/host profile."""

    profile_id: str
    platform: PlatformFamily
    python_minimum: CompatibilityVersion
    python_maximum: CompatibilityVersion | None
    host_version: CompatibilityVersion | None
    host_revision: str | None
    dependencies: tuple[DependencyConstraint, ...]
    required_native_node_ids: tuple[str, ...]
    forbidden_optional_imports: tuple[str, ...]
    declared_status: CompatibilityStatus
    evidence_level: str
    description: str

    def __post_init__(self) -> None:
        _bounded_identifier(self.profile_id, "profile_id")
        if not isinstance(self.platform, PlatformFamily):
            raise CompatibilityError("invalid_platform", "platform must be a PlatformFamily")
        if not isinstance(self.python_minimum, CompatibilityVersion):
            raise CompatibilityError("invalid_version", "python minimum is invalid")
        if self.python_maximum is not None and not isinstance(
            self.python_maximum, CompatibilityVersion
        ):
            raise CompatibilityError("invalid_version", "python maximum is invalid")
        if self.python_maximum is not None and self.python_minimum > self.python_maximum:
            raise CompatibilityError("invalid_version", "python range is reversed")
        if (self.host_version is None) != (self.host_revision is None):
            raise CompatibilityError(
                "invalid_host_requirement", "host version and revision must be paired"
            )
        if (
            self.host_revision is not None
            and _REVISION_PATTERN.fullmatch(self.host_revision) is None
        ):
            raise CompatibilityError("invalid_revision", "host revision must be a 40-character SHA")
        if len(self.dependencies) > MAX_COMPATIBILITY_DEPENDENCIES:
            raise CompatibilityError("limit_exceeded", "profile dependencies exceed the limit")
        dependency_names = tuple(item.name for item in self.dependencies)
        _tuple_unique(dependency_names, "profile dependencies", MAX_COMPATIBILITY_DEPENDENCIES)
        _tuple_unique(
            self.required_native_node_ids,
            "required native node IDs",
            MAX_COMPATIBILITY_NATIVE_NODES,
        )
        for node_id in self.required_native_node_ids:
            _bounded_identifier(node_id, "native node ID")
        _tuple_unique(
            self.forbidden_optional_imports,
            "forbidden optional imports",
            MAX_COMPATIBILITY_OPTIONAL_ROOTS,
        )
        for root in self.forbidden_optional_imports:
            _bounded_identifier(root, "optional import root", package=True)
        if not isinstance(self.declared_status, CompatibilityStatus):
            raise CompatibilityError("invalid_status", "declared status is invalid")
        _bounded_identifier(self.evidence_level, "evidence level")
        _bounded_message(self.description, "profile description")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "platform": self.platform.value,
            "python": {
                "minimum": str(self.python_minimum),
                "maximum": None if self.python_maximum is None else str(self.python_maximum),
            },
            "host": {
                "version": None if self.host_version is None else str(self.host_version),
                "revision_parts": _revision_parts(self.host_revision),
            },
            "dependencies": [item.to_wire() for item in self.dependencies],
            "required_native_node_ids": list(self.required_native_node_ids),
            "forbidden_optional_imports": list(self.forbidden_optional_imports),
            "status": self.declared_status.value,
            "evidence_level": self.evidence_level,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityMatrix:
    """Immutable collection of named compatibility profiles."""

    profiles: tuple[CompatibilityProfile, ...]
    schema: str = COMPATIBILITY_MATRIX_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != COMPATIBILITY_MATRIX_SCHEMA:
            raise CompatibilityError(
                "unsupported_schema", "unsupported compatibility matrix schema"
            )
        if not self.profiles or len(self.profiles) > MAX_COMPATIBILITY_PROFILES:
            raise CompatibilityError("invalid_matrix", "matrix must contain bounded profiles")
        if not all(isinstance(profile, CompatibilityProfile) for profile in self.profiles):
            raise CompatibilityError("invalid_matrix", "matrix contains an invalid profile")
        profile_ids = tuple(profile.profile_id for profile in self.profiles)
        _tuple_unique(profile_ids, "profile IDs", MAX_COMPATIBILITY_PROFILES)

    def profile(self, profile_id: str) -> CompatibilityProfile | None:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profiles": [profile.to_wire() for profile in self.profiles],
        }


@dataclass(frozen=True, slots=True)
class CompatibilityObservation:
    """Sanitized facts supplied by a read-only host/environment probe."""

    profile_id: str
    platform: PlatformFamily
    python_version: CompatibilityVersion
    host_version: CompatibilityVersion | None
    host_revision: str | None
    dependencies: tuple[tuple[str, CompatibilityVersion], ...]
    native_node_ids: tuple[str, ...]
    optional_imports: tuple[str, ...]
    foreign_registration_collisions: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_identifier(self.profile_id, "observation profile_id")
        if not isinstance(self.platform, PlatformFamily):
            raise CompatibilityError("invalid_platform", "observation platform is invalid")
        if not isinstance(self.python_version, CompatibilityVersion):
            raise CompatibilityError("invalid_version", "observed Python version is invalid")
        if (self.host_version is None) != (self.host_revision is None):
            raise CompatibilityError(
                "invalid_host_observation", "host version and revision must be paired"
            )
        if (
            self.host_revision is not None
            and _REVISION_PATTERN.fullmatch(self.host_revision) is None
        ):
            raise CompatibilityError("invalid_revision", "observed host revision is invalid")
        if len(self.dependencies) > MAX_COMPATIBILITY_DEPENDENCIES:
            raise CompatibilityError("limit_exceeded", "observed dependencies exceed the limit")
        dependency_names: list[str] = []
        for name, version in self.dependencies:
            dependency_names.append(_bounded_identifier(name, "observed dependency", package=True))
            if not isinstance(version, CompatibilityVersion):
                raise CompatibilityError(
                    "invalid_version", "observed dependency version is invalid"
                )
        _tuple_unique(
            tuple(dependency_names), "observed dependencies", MAX_COMPATIBILITY_DEPENDENCIES
        )
        for values, field, maximum in (
            (self.native_node_ids, "observed native node IDs", MAX_COMPATIBILITY_NATIVE_NODES),
            (self.optional_imports, "observed optional imports", MAX_COMPATIBILITY_OPTIONAL_ROOTS),
            (
                self.foreign_registration_collisions,
                "foreign registration collisions",
                MAX_COMPATIBILITY_NATIVE_NODES,
            ),
        ):
            _tuple_unique(values, field, maximum)
            for value in values:
                _bounded_identifier(value, field, package=field == "observed optional imports")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "platform": self.platform.value,
            "python_version": str(self.python_version),
            "host_version": None if self.host_version is None else str(self.host_version),
            "host_revision": self.host_revision,
            "dependencies": {name: str(version) for name, version in self.dependencies},
            "native_node_ids": list(self.native_node_ids),
            "optional_imports": list(self.optional_imports),
            "foreign_registration_collisions": list(self.foreign_registration_collisions),
        }


@dataclass(frozen=True, slots=True)
class CompatibilityDiagnostic:
    """Actionable, secret-free compatibility failure detail."""

    code: str
    message: str
    severity: str = "error"

    def __post_init__(self) -> None:
        _bounded_identifier(self.code, "diagnostic code")
        _bounded_message(self.message, "diagnostic message")
        if self.severity not in {"info", "warning", "error", "fatal"}:
            raise CompatibilityError("invalid_severity", "diagnostic severity is invalid")

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    """Result of comparing one observation with one declarative profile."""

    profile_id: str
    status: CompatibilityStatus
    diagnostics: tuple[CompatibilityDiagnostic, ...] = ()
    schema: str = COMPATIBILITY_MATRIX_SCHEMA

    def __post_init__(self) -> None:
        _bounded_identifier(self.profile_id, "assessment profile_id")
        if not isinstance(self.status, CompatibilityStatus):
            raise CompatibilityError("invalid_status", "assessment status is invalid")
        if self.schema != COMPATIBILITY_MATRIX_SCHEMA:
            raise CompatibilityError("unsupported_schema", "unsupported assessment schema")
        if len(self.diagnostics) > MAX_COMPATIBILITY_DIAGNOSTICS:
            raise CompatibilityError("limit_exceeded", "diagnostics exceed the bounded limit")
        if not all(isinstance(item, CompatibilityDiagnostic) for item in self.diagnostics):
            raise CompatibilityError("invalid_diagnostic", "assessment diagnostics are invalid")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "status": self.status.value,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


def _diagnostic(code: str, message: str, severity: str = "error") -> CompatibilityDiagnostic:
    return CompatibilityDiagnostic(code, message, severity)


def _dependency_map(
    dependencies: tuple[tuple[str, CompatibilityVersion], ...],
) -> dict[str, CompatibilityVersion]:
    return dict(dependencies)


def assess_compatibility(
    matrix: CompatibilityMatrix, observation: CompatibilityObservation
) -> CompatibilityAssessment:
    """Compare sanitized observed facts with the named profile and fail closed."""

    if not isinstance(matrix, CompatibilityMatrix):
        raise CompatibilityError("invalid_matrix", "matrix must be a CompatibilityMatrix")
    if not isinstance(observation, CompatibilityObservation):
        raise CompatibilityError(
            "invalid_observation", "observation must be a CompatibilityObservation"
        )
    profile = matrix.profile(observation.profile_id)
    if profile is None:
        return CompatibilityAssessment(
            observation.profile_id,
            CompatibilityStatus.UNSUPPORTED,
            (
                _diagnostic(
                    "unsupported_profile", "profile is not declared by the compatibility matrix"
                ),
            ),
        )

    diagnostics: list[CompatibilityDiagnostic] = []
    if profile.platform not in {PlatformFamily.ANY, observation.platform}:
        diagnostics.append(
            _diagnostic(
                "platform_mismatch",
                f"profile requires {profile.platform.value}; observed {observation.platform.value}",
            )
        )
    if observation.python_version < profile.python_minimum:
        diagnostics.append(
            _diagnostic(
                "python_below_minimum",
                f"Python {observation.python_version} is below minimum {profile.python_minimum}",
            )
        )
    if profile.python_maximum is not None and observation.python_version > profile.python_maximum:
        diagnostics.append(
            _diagnostic(
                "python_above_maximum",
                f"Python {observation.python_version} is above maximum {profile.python_maximum}",
            )
        )
    # Host identity is retained in the observation as provenance. Capability is
    # decided by native node and dependency observations below, never by a
    # version/revision equality against the historical matrix profile.
    if profile.host_version is None and (
        observation.host_version is not None or observation.host_revision is not None
    ):
        diagnostics.append(
            _diagnostic("unexpected_host_metadata", "pure-core profile must not include host facts")
        )

    observed_dependencies = _dependency_map(observation.dependencies)
    for constraint in profile.dependencies:
        observed = observed_dependencies.get(constraint.name)
        if observed is None:
            if not constraint.optional:
                diagnostics.append(
                    _diagnostic(
                        "dependency_missing", f"required dependency {constraint.name} is missing"
                    )
                )
            continue
        if not constraint.accepts(observed):
            if constraint.exact is not None:
                code = "dependency_exact_mismatch"
                expected = str(constraint.exact)
            elif constraint.minimum is not None and observed < constraint.minimum:
                code = "dependency_below_minimum"
                expected = f">={constraint.minimum}"
            else:
                code = "dependency_above_maximum"
                expected = f"<={constraint.maximum}"
            diagnostics.append(
                _diagnostic(
                    code,
                    f"dependency {constraint.name}={observed} does not satisfy {expected}",
                )
            )

    observed_nodes = set(observation.native_node_ids)
    for node_id in profile.required_native_node_ids:
        if node_id not in observed_nodes:
            diagnostics.append(
                _diagnostic("native_node_missing", f"native H3 node {node_id} is missing")
            )
    if observation.optional_imports:
        diagnostics.append(
            _diagnostic(
                "optional_import_leak",
                "optional runtime imports appeared during package probing",
            )
        )
    if observation.foreign_registration_collisions:
        diagnostics.append(
            _diagnostic(
                "foreign_registration_collision",
                "a co-installed node owns a project registration ID or display entry",
            )
        )

    if diagnostics:
        return CompatibilityAssessment(
            observation.profile_id,
            CompatibilityStatus.UNSUPPORTED,
            tuple(diagnostics),
        )
    if profile.declared_status in {
        CompatibilityStatus.UNSUPPORTED,
        CompatibilityStatus.BLOCKED,
    }:
        return CompatibilityAssessment(
            observation.profile_id,
            profile.declared_status,
            (_diagnostic("profile_not_supported", "profile is declared non-supported"),),
        )
    return CompatibilityAssessment(observation.profile_id, profile.declared_status)


def default_compatibility_matrix() -> CompatibilityMatrix:
    """Return the reviewed offline-core and pinned ComfyUI reference profiles."""

    return CompatibilityMatrix(
        profiles=(
            CompatibilityProfile(
                profile_id="linux-wsl-core",
                platform=PlatformFamily.LINUX_WSL,
                python_minimum=CompatibilityVersion(3, 10, 0),
                python_maximum=None,
                host_version=None,
                host_revision=None,
                dependencies=(),
                required_native_node_ids=(),
                forbidden_optional_imports=(
                    "aiohttp",
                    "comfy",
                    "comfy_api",
                    "diffusers",
                    "httpx",
                    "moviepy",
                    "requests",
                    "torch",
                    "torchaudio",
                    "transformers",
                ),
                declared_status=CompatibilityStatus.SUPPORTED,
                evidence_level="framework_reference",
                description="Offline pure-core and package import without optional runtime stacks.",
            ),
            CompatibilityProfile(
                profile_id="windows-core",
                platform=PlatformFamily.WINDOWS,
                python_minimum=CompatibilityVersion(3, 10, 0),
                python_maximum=None,
                host_version=None,
                host_revision=None,
                dependencies=(),
                required_native_node_ids=(),
                forbidden_optional_imports=(
                    "aiohttp",
                    "comfy",
                    "comfy_api",
                    "diffusers",
                    "httpx",
                    "moviepy",
                    "requests",
                    "torch",
                    "torchaudio",
                    "transformers",
                ),
                declared_status=CompatibilityStatus.SUPPORTED,
                evidence_level="framework_reference",
                description="Offline pure-core and package import on a Windows interpreter.",
            ),
            CompatibilityProfile(
                profile_id="comfyui-reference-0.32.0",
                platform=PlatformFamily.ANY,
                python_minimum=CompatibilityVersion(3, 10, 0),
                python_maximum=None,
                host_version=CompatibilityVersion(0, 32, 0),
                host_revision=(
                    "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
                ),
                dependencies=(
                    DependencyConstraint("comfy-aimdo", exact=CompatibilityVersion(0, 4, 11)),
                    DependencyConstraint("comfy-kitchen", exact=CompatibilityVersion(0, 2, 26)),
                    DependencyConstraint("torch", minimum=CompatibilityVersion(2, 4, 0)),
                    DependencyConstraint("transformers", minimum=CompatibilityVersion(4, 50, 3)),
                ),
                required_native_node_ids=(
                    "MiniMaxH3ImageToVideo",
                    "MiniMaxH3ReferenceToVideo",
                ),
                forbidden_optional_imports=(),
                declared_status=CompatibilityStatus.SUPPORTED,
                evidence_level="framework_reference",
                description="Pinned model-free ComfyUI H0/H1 host and native MiniMax H3 wiring.",
            ),
        )
    )


__all__ = [
    "COMPATIBILITY_MATRIX_SCHEMA",
    "CompatibilityAssessment",
    "CompatibilityDiagnostic",
    "CompatibilityError",
    "CompatibilityMatrix",
    "CompatibilityObservation",
    "CompatibilityProfile",
    "CompatibilityStatus",
    "CompatibilityVersion",
    "DependencyConstraint",
    "PlatformFamily",
    "assess_compatibility",
    "default_compatibility_matrix",
]
