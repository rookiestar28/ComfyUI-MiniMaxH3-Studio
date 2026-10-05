"""Deterministic H3 request normalization without media or runtime dependencies.

The normalizer is intentionally a policy boundary, not a prompt renderer. It validates typed asset
descriptors and user-declared mode/duration inputs, derives the pinned H3 frame grid, and returns
typed diagnostics. An error never produces a plausible normalized request.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from math import isfinite

from .constraints import HardConstraintSet, normalize_constraints
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    AssetDescriptor,
    AssetRole,
    MediaKind,
    ModelVariant,
    ProfileIdentity,
    PromptProfile,
    SchemaVersion,
    TaskMode,
    ValidationDiagnostic,
    ValidationSeverity,
)
from .errors import ContractValidationError
from .evidence import EvidenceSet
from .length import (
    DEFAULT_FRAME_COUNT,
    FPS,
    MAX_FRAME_COUNT,
    MILLISECONDS_PER_SECOND,
    MIN_FRAME_COUNT,
    TRAINED_MAX_FRAME_COUNT,
    TRAINED_MIN_FRAME_COUNT,
    LengthError,
    is_producible,
    resolve_milliseconds,
)
from .registry import ReferenceRegistry

# M17-25: the lattice, the ranges and the conversions live in `.length`, which is
# the single alignment authority. These names are re-exported rather than
# redefined so existing importers keep working, and so no second copy of the
# arithmetic can drift from the first.
MAX_USER_INTENT_LENGTH = 65_536
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIO = 6


class DurationSource(str, Enum):
    """The explicit source selected for effective frame-count derivation."""

    DEFAULT = "default"
    SECONDS = "seconds"


@dataclass(frozen=True, slots=True)
class RawContextRequest:
    """Typed caller input before normalization; values are not inspected as media."""

    mode: TaskMode | str
    user_intent: str
    duration_seconds: int | float | None = None
    assets: tuple[AssetDescriptor, ...] = ()
    hard_constraints: HardConstraintSet = HardConstraintSet()
    reference_registry: ReferenceRegistry = ReferenceRegistry.empty()
    evidence: EvidenceSet = EvidenceSet.empty()

    def to_wire(self) -> dict[str, object]:
        """Return a JSON-safe copy without normalizing or rewriting caller controls."""

        return {
            "mode": self.mode.value if isinstance(self.mode, TaskMode) else self.mode,
            "user_intent": self.user_intent,
            "duration_seconds": self.duration_seconds,
            "assets": [asset.to_wire() for asset in self.assets],
            "hard_constraints": self.hard_constraints.to_wire(),
            "reference_registry": self.reference_registry.to_wire(),
            "evidence": self.evidence.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class NormalizedContextRequest:
    """Provider-free normalized request with both requested and effective duration values."""

    schema_version: SchemaVersion
    profile: ProfileIdentity
    task_mode: TaskMode
    model_variant: ModelVariant
    user_intent: str
    requested_duration_seconds: float | None
    effective_frame_count: int
    effective_duration_seconds: float
    duration_source: DurationSource
    assets: tuple[AssetDescriptor, ...]
    hard_constraints: HardConstraintSet = HardConstraintSet()
    reference_registry: ReferenceRegistry = ReferenceRegistry.empty()
    evidence: EvidenceSet = EvidenceSet.empty()

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ContractValidationError("normalized request uses an unsupported schema version")
        if not isinstance(self.profile, ProfileIdentity):
            raise ContractValidationError("profile must be a ProfileIdentity")
        if not isinstance(self.task_mode, TaskMode):
            raise ContractValidationError("task_mode must be a TaskMode")
        if not isinstance(self.model_variant, ModelVariant):
            raise ContractValidationError("model_variant must be a ModelVariant")
        if not isinstance(self.user_intent, str) or not self.user_intent:
            raise ContractValidationError("normalized user_intent must be non-empty")
        if self.requested_duration_seconds is not None and (
            not isfinite(self.requested_duration_seconds) or self.requested_duration_seconds <= 0
        ):
            raise ContractValidationError("requested_duration_seconds must be finite and positive")
        if not is_producible(self.effective_frame_count):
            raise ContractValidationError("effective_frame_count is outside the H3 17k+5 grid")
        if not isfinite(self.effective_duration_seconds) or self.effective_duration_seconds <= 0:
            raise ContractValidationError("effective_duration_seconds must be finite and positive")
        if not isinstance(self.duration_source, DurationSource):
            raise ContractValidationError("duration_source must be a DurationSource")
        if not isinstance(self.assets, tuple) or not all(
            isinstance(asset, AssetDescriptor) for asset in self.assets
        ):
            raise ContractValidationError("assets must be a tuple of AssetDescriptor values")
        if not isinstance(self.hard_constraints, HardConstraintSet):
            raise ContractValidationError("hard_constraints must be a HardConstraintSet")
        if not isinstance(self.reference_registry, ReferenceRegistry):
            raise ContractValidationError("reference_registry must be a ReferenceRegistry")
        if not isinstance(self.evidence, EvidenceSet):
            raise ContractValidationError("evidence must be an EvidenceSet")

    def to_wire(self) -> dict[str, object]:
        """Return a JSON-compatible copy for report envelopes and audit output."""

        return {
            "schema_version": str(self.schema_version),
            "profile": self.profile.to_wire(),
            "task_mode": self.task_mode.value,
            "model_variant": self.model_variant.value,
            "user_intent": self.user_intent,
            "requested_duration_seconds": self.requested_duration_seconds,
            "effective_frame_count": self.effective_frame_count,
            "effective_duration_seconds": self.effective_duration_seconds,
            "duration_source": self.duration_source.value,
            "assets": [asset.to_wire() for asset in self.assets],
            "hard_constraints": self.hard_constraints.to_wire(),
            "reference_registry": self.reference_registry.to_wire(),
            "evidence": self.evidence.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Normalized request plus diagnostics; error diagnostics always leave ``request`` absent."""

    request: NormalizedContextRequest | None
    diagnostics: tuple[ValidationDiagnostic, ...]

    @property
    def has_errors(self) -> bool:
        return any(
            diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
            for diagnostic in self.diagnostics
        )

    @property
    def is_valid(self) -> bool:
        return self.request is not None and not self.has_errors


def _diagnostic(
    severity: ValidationSeverity,
    code: str,
    message: str,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(severity=severity, code=code, message=message)


def _resolve_mode(
    raw_mode: TaskMode | str, diagnostics: list[ValidationDiagnostic]
) -> TaskMode | None:
    if isinstance(raw_mode, TaskMode):
        return raw_mode
    if not isinstance(raw_mode, str):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR, "unsupported_task_mode", "task mode is not a string"
            )
        )
        return None
    try:
        return TaskMode(raw_mode)
    except ValueError:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "unsupported_task_mode",
                f"unsupported H3 task mode: {raw_mode!r}",
            )
        )
        return None


def _validate_user_intent(raw_intent: object, diagnostics: list[ValidationDiagnostic]) -> bool:
    if not isinstance(raw_intent, str):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR, "invalid_user_intent", "user_intent must be a string"
            )
        )
        return False
    if not raw_intent.strip():
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR, "missing_user_intent", "user_intent must not be blank"
            )
        )
        return False
    if len(raw_intent) > MAX_USER_INTENT_LENGTH:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "user_intent_too_long",
                f"user_intent exceeds {MAX_USER_INTENT_LENGTH} characters",
            )
        )
        return False
    if any((ord(char) < 0x20 and char not in "\n\r\t") or ord(char) == 0x7F for char in raw_intent):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "invalid_user_intent",
                "user_intent contains a forbidden control character",
            )
        )
        return False
    return True


def _normalize_duration(
    raw: RawContextRequest,
    diagnostics: list[ValidationDiagnostic],
) -> tuple[float | None, int, DurationSource] | None:
    """Resolve the single authored length into a producible frame count.

    M17-25: duration is the only authored length. The conversion is delegated to
    `.length`, the single alignment authority, so the Context path and the
    Production segment workspace cannot drift apart.
    """

    if raw.duration_seconds is None:
        return None, DEFAULT_FRAME_COUNT, DurationSource.DEFAULT

    seconds = raw.duration_seconds
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duration_out_of_bounds",
                "duration_seconds must be numeric",
            )
        )
        return None
    try:
        seconds_float = float(seconds)
    except OverflowError:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duration_out_of_bounds",
                "duration_seconds is outside the representable range",
            )
        )
        return None
    if not isfinite(seconds_float) or seconds_float <= 0:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duration_out_of_bounds",
                "duration_seconds must be finite and positive",
            )
        )
        return None

    # The wire value is integer milliseconds, which is also what makes the
    # rounding mode unobservable: `ms * 24 / 1000 = x.5` needs `6 * ms` (even) to
    # equal `250x + 125` (odd), so an exact tie is unreachable and ROUND_HALF_EVEN
    # never has to break one.
    try:
        milliseconds = int(
            (Decimal(str(seconds)) * MILLISECONDS_PER_SECOND).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duration_out_of_bounds",
                "duration_seconds is not representable",
            )
        )
        return None

    try:
        resolved = resolve_milliseconds(milliseconds)
    except LengthError as error:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duration_out_of_bounds",
                "duration_seconds rounds above the H3 host maximum"
                if error.code == "duration_above_maximum"
                else "duration_seconds maps outside the "
                f"{MIN_FRAME_COUNT}..{MAX_FRAME_COUNT} frame bound",
            )
        )
        return None

    # Compared against the request, not against the intermediate estimate. The
    # pre-M17-25 warning compared aligned against raw, so a 4.46 s request -- raw
    # and aligned both 107 frames, delivered 4.458 s -- moved silently.
    if resolved.snapped:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.WARNING,
                "duration_snapped",
                f"requested {resolved.requested_milliseconds} ms delivers "
                f"{resolved.delivered_milliseconds} ms ({resolved.frame_count} frames), "
                f"{resolved.direction} than requested",
            )
        )
    return seconds_float, resolved.frame_count, DurationSource.SECONDS


def _validate_assets(
    mode: TaskMode,
    raw_assets: object,
    diagnostics: list[ValidationDiagnostic],
) -> tuple[AssetDescriptor, ...] | None:
    if not isinstance(raw_assets, tuple) or not all(
        isinstance(asset, AssetDescriptor) for asset in raw_assets
    ):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "invalid_assets",
                "assets must be a tuple of AssetDescriptor values",
            )
        )
        return None
    assets = tuple(raw_assets)
    identifiers = [asset.asset_id for asset in assets]
    if len(identifiers) != len(set(identifiers)):
        diagnostics.append(
            _diagnostic(ValidationSeverity.ERROR, "duplicate_asset_id", "asset IDs must be unique")
        )

    if mode is TaskMode.REF2VA:
        if not assets:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "missing_reference_asset",
                    "ref2va requires at least one reference asset",
                )
            )
            return None
        counts = {
            "image": sum(asset.kind is MediaKind.IMAGE for asset in assets),
            "video": sum(asset.kind is MediaKind.VIDEO for asset in assets),
            "audio": sum(asset.kind is MediaKind.AUDIO for asset in assets),
        }
        limits = {
            "image": (MAX_REFERENCE_IMAGES, "too_many_images"),
            "video": (MAX_REFERENCE_VIDEOS, "too_many_videos"),
            "audio": (MAX_REFERENCE_AUDIO, "too_many_audio"),
        }
        for kind, (limit, code) in limits.items():
            if counts[kind] > limit:
                diagnostics.append(
                    _diagnostic(
                        ValidationSeverity.ERROR,
                        code,
                        f"ref2va accepts at most {limit} {kind} assets",
                    )
                )
        return assets

    expected_roles: dict[TaskMode, frozenset[AssetRole]] = {
        TaskMode.T2VA: frozenset(),
        TaskMode.I2VA: frozenset({AssetRole.FIRST_FRAME}),
        TaskMode.FL2VA: frozenset({AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}),
        TaskMode.L2VA: frozenset({AssetRole.LAST_FRAME}),
    }
    allowed_roles = expected_roles[mode]
    if assets and mode is TaskMode.T2VA:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "unexpected_assets",
                "t2va does not accept reference assets",
            )
        )
    for asset in assets:
        if asset.role not in allowed_roles:
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "unexpected_assets",
                    f"{mode.value} does not accept asset role {asset.role.value}",
                )
            )
        if (
            asset.role in {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME}
            and asset.kind is not MediaKind.IMAGE
        ):
            diagnostics.append(
                _diagnostic(
                    ValidationSeverity.ERROR,
                    "frame_asset_must_be_image",
                    "Base frame anchors must be image assets",
                )
            )

    role_counts = {role: sum(asset.role is role for asset in assets) for role in allowed_roles}
    if AssetRole.FIRST_FRAME in allowed_roles and role_counts[AssetRole.FIRST_FRAME] == 0:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR, "missing_first_frame", "a first-frame image is required"
            )
        )
    if AssetRole.LAST_FRAME in allowed_roles and role_counts[AssetRole.LAST_FRAME] == 0:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR, "missing_last_frame", "a last-frame image is required"
            )
        )
    if role_counts.get(AssetRole.FIRST_FRAME, 0) > 1:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duplicate_first_frame",
                "only one first-frame image is allowed",
            )
        )
    if role_counts.get(AssetRole.LAST_FRAME, 0) > 1:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "duplicate_last_frame",
                "only one last-frame image is allowed",
            )
        )
    return assets


def _validate_hard_constraints(
    raw_constraints: object,
    diagnostics: list[ValidationDiagnostic],
) -> HardConstraintSet | None:
    try:
        return normalize_constraints(raw_constraints)  # type: ignore[arg-type]
    except ContractValidationError as exc:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "invalid_hard_constraints",
                str(exc),
            )
        )
        return None


def _validate_reference_registry(
    raw_registry: object,
    diagnostics: list[ValidationDiagnostic],
) -> ReferenceRegistry | None:
    if not isinstance(raw_registry, ReferenceRegistry):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "invalid_reference_registry",
                "reference_registry must be a ReferenceRegistry",
            )
        )
        return None
    return raw_registry


def _validate_evidence(
    raw_evidence: object,
    diagnostics: list[ValidationDiagnostic],
) -> EvidenceSet | None:
    if not isinstance(raw_evidence, EvidenceSet):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "invalid_evidence",
                "evidence must be an EvidenceSet",
            )
        )
        return None
    return raw_evidence


def validate_request_controls(raw: RawContextRequest) -> tuple[ValidationDiagnostic, ...]:
    """Validate mode, text, duration, and typed controls before reference binding.

    This deliberately omits mode-specific asset validation. A request node must be able to carry
    an ``i2va``/``fl2va``/``l2va``/``ref2va`` mode before the later reference-registry node binds
    assets; ``normalize_request`` remains the authority for the complete request.
    """

    if not isinstance(raw, RawContextRequest):
        raise ContractValidationError("request controls must be a RawContextRequest")
    diagnostics: list[ValidationDiagnostic] = []
    _resolve_mode(raw.mode, diagnostics)
    _validate_user_intent(raw.user_intent, diagnostics)
    _normalize_duration(raw, diagnostics)
    _validate_hard_constraints(raw.hard_constraints, diagnostics)
    _validate_reference_registry(raw.reference_registry, diagnostics)
    _validate_evidence(raw.evidence, diagnostics)
    return tuple(diagnostics)


def normalize_request(raw: RawContextRequest) -> NormalizationResult:
    """Normalize a raw request deterministically and fail closed on any error diagnostic."""

    diagnostics: list[ValidationDiagnostic] = []
    mode = _resolve_mode(raw.mode, diagnostics)
    intent_valid = _validate_user_intent(raw.user_intent, diagnostics)
    duration = _normalize_duration(raw, diagnostics)
    hard_constraints = _validate_hard_constraints(raw.hard_constraints, diagnostics)
    reference_registry = _validate_reference_registry(raw.reference_registry, diagnostics)
    evidence = _validate_evidence(raw.evidence, diagnostics)
    asset_input: object = raw.assets
    if reference_registry is not None and reference_registry.assets and not raw.assets:
        asset_input = reference_registry.to_asset_descriptors()
    elif (
        reference_registry is not None
        and reference_registry.assets
        and tuple(raw.assets) != reference_registry.to_asset_descriptors()
    ):
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.ERROR,
                "asset_registry_mismatch",
                "legacy assets and reference_registry must describe the same ordered assets",
            )
        )
    assets = _validate_assets(mode, asset_input, diagnostics) if mode is not None else None
    if (
        mode is None
        or not intent_valid
        or duration is None
        or assets is None
        or hard_constraints is None
        or reference_registry is None
        or evidence is None
    ):
        return NormalizationResult(request=None, diagnostics=tuple(diagnostics))
    if any(
        diagnostic.severity in {ValidationSeverity.ERROR, ValidationSeverity.FATAL}
        for diagnostic in diagnostics
    ):
        return NormalizationResult(request=None, diagnostics=tuple(diagnostics))

    requested_seconds, effective_frames, duration_source = duration
    if not TRAINED_MIN_FRAME_COUNT <= effective_frames <= TRAINED_MAX_FRAME_COUNT:
        diagnostics.append(
            _diagnostic(
                ValidationSeverity.WARNING,
                "untested_duration",
                f"{effective_frames} frames is outside the inspected trained range "
                f"{TRAINED_MIN_FRAME_COUNT}..{TRAINED_MAX_FRAME_COUNT}",
            )
        )

    if mode is TaskMode.REF2VA:
        profile = ProfileIdentity(PromptProfile.FULL_REFERENCE, CURRENT_SCHEMA_VERSION)
        model_variant = ModelVariant.BASE_REF2VA
    else:
        profile = ProfileIdentity(PromptProfile.BASE, CURRENT_SCHEMA_VERSION)
        model_variant = ModelVariant.BASE_FL2VA

    request = NormalizedContextRequest(
        schema_version=CURRENT_SCHEMA_VERSION,
        profile=profile,
        task_mode=mode,
        model_variant=model_variant,
        user_intent=raw.user_intent,
        requested_duration_seconds=requested_seconds,
        effective_frame_count=effective_frames,
        effective_duration_seconds=effective_frames / FPS,
        duration_source=duration_source,
        assets=assets,
        hard_constraints=hard_constraints,
        reference_registry=reference_registry,
        evidence=evidence,
    )
    return NormalizationResult(request=request, diagnostics=tuple(diagnostics))


__all__ = [
    "DEFAULT_FRAME_COUNT",
    "FPS",
    "MAX_FRAME_COUNT",
    "MAX_REFERENCE_AUDIO",
    "MAX_REFERENCE_IMAGES",
    "MAX_REFERENCE_VIDEOS",
    "MAX_USER_INTENT_LENGTH",
    "MIN_FRAME_COUNT",
    "NormalizationResult",
    "NormalizedContextRequest",
    "RawContextRequest",
    "TRAINED_MAX_FRAME_COUNT",
    "TRAINED_MIN_FRAME_COUNT",
    "DurationSource",
    "normalize_request",
    "validate_request_controls",
]
