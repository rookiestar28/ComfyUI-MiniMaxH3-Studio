"""Provider-free wiring contract for the pinned native MiniMax H3 nodes.

The adapter describes how a compiled prompt and already-owned media slots connect to native
conditioning nodes. It never receives, opens, transforms, hashes, or uploads media values.
"""

from __future__ import annotations

import re
import weakref
from dataclasses import dataclass
from decimal import Decimal

from .canonical import canonical_fingerprint
from .context_reporting import ContextReport, ValidationStatus
from .contracts import AssetRole, MediaKind, ProfileIdentity, PromptProfile, TaskMode
from .errors import ContextReportError, NativeH3AdapterError, ReportLifecycleError
from .length import is_producible
from .registry import BackendLabel, ReferenceRegistry
from .validation_lifecycle import ValidatedReportEnvelope, require_execution_ready

NATIVE_H3_WIRING_SCHEMA = "h3-native-h3-wiring/1"
NATIVE_H3_HOST_VERSION = "0.32.0"
NATIVE_H3_HOST_REVISION = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
NATIVE_H3_SOURCE = "comfy_extras/nodes_minimax_h3.py"
NATIVE_H3_SOURCE_BLOB = "0b1840e851c248f89e9920159c3c8237fa2e7186"  # pragma: allowlist secret
NATIVE_H3_IMAGE_NODE_ID = "MiniMaxH3ImageToVideo"
NATIVE_H3_REFERENCE_NODE_ID = "MiniMaxH3ReferenceToVideo"
NATIVE_H3_MAX_BINDINGS = 32
NATIVE_H3_BASE_MAX_REFERENCES = 12
NATIVE_H3_BASE_MAX_IMAGES = 9
NATIVE_H3_BASE_MAX_VIDEOS = 3
NATIVE_H3_BASE_MAX_AUDIO = 3
NATIVE_H3_BASE_MIN_OUTPUT_SECONDS = Decimal("4")
NATIVE_H3_BASE_MAX_OUTPUT_SECONDS = Decimal("15")
NATIVE_H3_BASE_MIN_TIMED_REFERENCE_SECONDS = Decimal("2")
NATIVE_H3_BASE_MAX_TIMED_REFERENCE_SECONDS = Decimal("15")
NATIVE_H3_BASE_MAX_VIDEO_TOTAL_SECONDS = Decimal("15")
NATIVE_H3_BASE_MAX_AUDIO_TOTAL_SECONDS = Decimal("15")
NATIVE_H3_DURATION_UNVERIFIED = "h3_base_reference_duration_metadata_unverified"
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_PROMPT_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NATIVE_INPUT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_NATIVE_IMAGE_INPUTS = (
    "clip",
    "vae",
    "prompt",
    "width",
    "height",
    "length",
    "first_frame",
    "last_frame",
)
_NATIVE_REFERENCE_INPUTS = (
    "clip",
    "vae",
    "audio_vae",
    "prompt",
    "width",
    "height",
    "length",
    "ref_image_size",
    "ref_images",
    "ref_videos",
    "ref_video_audios",
    "ref_audios",
)
_MEDIA_FLOW = "direct_to_native"
_MEDIA_PROCESSING = "native_only"
_UPLOAD_PATH = "none"


def _require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise NativeH3AdapterError(
            "invalid_native_metadata", f"{field} is not a bounded identifier"
        )
    return value


def _require_native_input(value: object, field: str) -> str:
    if not isinstance(value, str) or _NATIVE_INPUT_PATTERN.fullmatch(value) is None:
        raise NativeH3AdapterError("invalid_native_metadata", f"{field} is not a native input name")
    return value


def _expected_label(kind: MediaKind, ordinal: int) -> str:
    prefix = {
        MediaKind.IMAGE: "Picture",
        MediaKind.VIDEO: "Video",
        MediaKind.AUDIO: "Audio",
    }[kind]
    return f"<{prefix} {ordinal}>"


def _label_ordinal(label: BackendLabel, expected_kind: MediaKind) -> int:
    if (
        label.kind.value
        != {
            MediaKind.IMAGE: "picture",
            MediaKind.VIDEO: "video",
            MediaKind.AUDIO: "audio",
        }[expected_kind]
    ):
        raise NativeH3AdapterError(
            "reference_label_kind_mismatch", "reference label kind mismatches asset"
        )
    if label.label != _expected_label(expected_kind, label.ordinal):
        raise NativeH3AdapterError(
            "reference_label_mismatch", "reference label is not native H3 vocabulary"
        )
    return label.ordinal


@dataclass(frozen=True, slots=True)
class NativeH3Binding:
    """One canonical asset-to-native input mapping with no media payload."""

    asset_id: str
    kind: MediaKind
    label: str
    ordinal: int
    native_input: str
    native_slot: str

    def __post_init__(self) -> None:
        _require_identifier(self.asset_id, "binding asset_id")
        if not isinstance(self.kind, MediaKind):
            raise NativeH3AdapterError("invalid_binding", "binding kind is not a MediaKind")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or not 1 <= self.ordinal <= 256
        ):
            raise NativeH3AdapterError(
                "invalid_binding", "binding ordinal is outside the bounded range"
            )
        if not isinstance(self.label, str) or self.label != _expected_label(
            self.kind, self.ordinal
        ):
            raise NativeH3AdapterError(
                "invalid_binding", "binding label does not match its kind and ordinal"
            )
        _require_native_input(self.native_input, "binding native_input")
        _require_native_input(self.native_slot, "binding native_slot")

    def to_wire(self) -> dict[str, str | int]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "label": self.label,
            "ordinal": self.ordinal,
            "native_input": self.native_input,
            "native_slot": self.native_slot,
            "native_path": self.native_path,
        }

    @property
    def native_path(self) -> str:
        """Return the transport identity without conflating it with presentation labels."""

        if self.native_input in {
            "ref_images",
            "ref_videos",
            "ref_video_audios",
            "ref_audios",
        }:
            # IMPORTANT: V3 Autogrow child paths are fully qualified and zero-based; prompt
            # labels remain independently one-based.
            return f"{self.native_input}.{self.native_slot}"
        return self.native_slot


# CRITICAL: keep the standard layout; Python 3.10 has no weakref_slot and this value is
# retained by a weak-reference registry.
@dataclass(frozen=True)
class NativeH3Wiring:
    """Immutable, JSON-safe manifest for direct prompt/media wiring into native H3."""

    report_id: str
    host_version: str
    host_revision: str
    native_source: str
    native_source_blob: str
    native_node_id: str
    task_mode: TaskMode
    profile: ProfileIdentity
    prompt: str
    prompt_fingerprint: str
    native_length_frames: int
    native_input_names: tuple[str, ...]
    bindings: tuple[NativeH3Binding, ...] = ()
    validation_status: ValidationStatus = ValidationStatus.NOT_RUN
    limitations: tuple[str, ...] = ()
    schema: str = NATIVE_H3_WIRING_SCHEMA
    media_flow: str = _MEDIA_FLOW
    media_processing: str = _MEDIA_PROCESSING
    upload_path: str = _UPLOAD_PATH

    def __post_init__(self) -> None:
        if self.schema != NATIVE_H3_WIRING_SCHEMA:
            raise NativeH3AdapterError(
                "unsupported_wiring_schema", "unsupported native wiring schema"
            )
        _require_identifier(self.report_id, "wiring report_id")
        if (
            not isinstance(self.host_version, str)
            or len(self.host_version) > 64
            or _VERSION_PATTERN.fullmatch(self.host_version) is None
        ):
            raise NativeH3AdapterError(
                "invalid_host_version", "native H3 host version is malformed"
            )
        if (
            not isinstance(self.host_revision, str)
            or _HASH_PATTERN.fullmatch(self.host_revision) is None
        ):
            raise NativeH3AdapterError(
                "invalid_host_revision", "native H3 host revision is malformed"
            )
        if self.native_source != NATIVE_H3_SOURCE or not isinstance(self.native_source_blob, str):
            raise NativeH3AdapterError(
                "invalid_native_source", "native H3 source metadata is not pinned"
            )
        if _HASH_PATTERN.fullmatch(self.native_source_blob) is None:
            raise NativeH3AdapterError(
                "invalid_native_source", "native H3 source blob is malformed"
            )
        if not isinstance(self.native_node_id, str) or self.native_node_id not in {
            NATIVE_H3_IMAGE_NODE_ID,
            NATIVE_H3_REFERENCE_NODE_ID,
        }:
            raise NativeH3AdapterError("unsupported_native_node", "native H3 node ID is not pinned")
        if not isinstance(self.task_mode, TaskMode):
            raise NativeH3AdapterError("invalid_task_mode", "native task mode is not typed")
        if not isinstance(self.profile, ProfileIdentity):
            raise NativeH3AdapterError("invalid_profile", "native profile is not typed")
        if (
            self.profile.name is PromptProfile.FULL_REFERENCE
            and self.task_mode is not TaskMode.REF2VA
        ):
            raise NativeH3AdapterError(
                "profile_task_mode_mismatch", "full-reference profile requires ref2va"
            )
        if self.profile.name is PromptProfile.BASE and self.task_mode is TaskMode.REF2VA:
            raise NativeH3AdapterError(
                "profile_task_mode_mismatch", "base profile does not support ref2va"
            )
        expected_node = (
            NATIVE_H3_REFERENCE_NODE_ID
            if self.task_mode is TaskMode.REF2VA
            else NATIVE_H3_IMAGE_NODE_ID
        )
        if self.native_node_id != expected_node:
            raise NativeH3AdapterError(
                "native_task_mode_mismatch", "native node does not match task mode"
            )
        if not isinstance(self.prompt, str) or not self.prompt:
            raise NativeH3AdapterError("invalid_prompt", "native prompt is empty")
        if len(self.prompt) > 65_536:
            raise NativeH3AdapterError("invalid_prompt", "native prompt exceeds the bounded limit")
        if _PROMPT_FINGERPRINT_PATTERN.fullmatch(self.prompt_fingerprint) is None:
            raise NativeH3AdapterError(
                "invalid_prompt_fingerprint", "native prompt fingerprint is malformed"
            )
        if self.prompt_fingerprint != canonical_fingerprint(self.prompt):
            raise NativeH3AdapterError(
                "prompt_fingerprint_mismatch", "native prompt fingerprint does not match prompt"
            )
        if not is_producible(self.native_length_frames):
            raise NativeH3AdapterError(
                "invalid_native_length", "native length is not a producible H3 length"
            )
        if not isinstance(self.native_input_names, tuple) or not self.native_input_names:
            raise NativeH3AdapterError(
                "invalid_native_inputs", "native input names must be a non-empty tuple"
            )
        if not all(
            isinstance(name, str) and _NATIVE_INPUT_PATTERN.fullmatch(name)
            for name in self.native_input_names
        ):
            raise NativeH3AdapterError("invalid_native_inputs", "native input names are malformed")
        expected_inputs = (
            (NATIVE_H3_IMAGE_NODE_ID, _NATIVE_IMAGE_INPUTS),
            (NATIVE_H3_REFERENCE_NODE_ID, _NATIVE_REFERENCE_INPUTS),
        )
        if (self.native_node_id, self.native_input_names) not in expected_inputs:
            raise NativeH3AdapterError(
                "native_input_mismatch", "native input names do not match native node"
            )
        if not isinstance(self.bindings, tuple) or len(self.bindings) > NATIVE_H3_MAX_BINDINGS:
            raise NativeH3AdapterError(
                "too_many_bindings", "native media bindings exceed the limit"
            )
        if not all(isinstance(item, NativeH3Binding) for item in self.bindings):
            raise NativeH3AdapterError(
                "invalid_binding", "native media bindings contain an invalid value"
            )
        asset_ids = tuple(item.asset_id for item in self.bindings)
        if len(asset_ids) != len(set(asset_ids)):
            raise NativeH3AdapterError(
                "duplicate_binding", "native media bindings duplicate an asset"
            )
        if self.validation_status is not ValidationStatus.PASSED:
            raise NativeH3AdapterError(
                "invalid_validation_status", "native wiring requires passed validation"
            )
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, str) and item and len(item) <= 256 for item in self.limitations
        ):
            raise NativeH3AdapterError("invalid_limitations", "native limitations are malformed")
        if (
            self.media_flow != _MEDIA_FLOW
            or self.media_processing != _MEDIA_PROCESSING
            or self.upload_path != _UPLOAD_PATH
        ):
            raise NativeH3AdapterError(
                "unsafe_media_policy", "native media policy is not direct and upload-free"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "host_version": self.host_version,
            "host_revision": self.host_revision,
            "native_source": self.native_source,
            "native_source_blob": self.native_source_blob,
            "native_node_id": self.native_node_id,
            "task_mode": self.task_mode.value,
            "profile": self.profile.to_wire(),
            "prompt": self.prompt,
            "prompt_fingerprint": self.prompt_fingerprint,
            "native_length_frames": self.native_length_frames,
            "native_input_names": list(self.native_input_names),
            "bindings": [item.to_wire() for item in self.bindings],
            "validation_status": self.validation_status.value,
            "limitations": list(self.limitations),
            "queue_ready": self.queue_ready,
            "media_flow": self.media_flow,
            "media_processing": self.media_processing,
            "upload_path": self.upload_path,
        }

    @property
    def queue_ready(self) -> bool:
        """Report whether admitted metadata proves the provider-free native envelope."""

        return NATIVE_H3_DURATION_UNVERIFIED not in self.limitations


def _validate_report(
    value: ContextReport | ValidatedReportEnvelope,
    *,
    expected_revision: int | None = None,
    expected_report_fingerprint: str | None = None,
) -> ContextReport:
    try:
        report = require_execution_ready(
            value,
            expected_revision=expected_revision,
            expected_report_fingerprint=expected_report_fingerprint,
        )
    except ReportLifecycleError as exc:
        raise NativeH3AdapterError(
            exc.code, "native adapter requires a current passed report"
        ) from exc
    registry = report.request.reference_registry
    if report.request.assets != registry.to_asset_descriptors():
        raise NativeH3AdapterError(
            "reference_registry_mismatch", "report assets do not match its registry"
        )
    return report


def _binding_for_label(
    asset_id: str,
    kind: MediaKind,
    label: BackendLabel,
    native_input: str,
    native_slot: str,
) -> NativeH3Binding:
    ordinal = _label_ordinal(label, kind)
    return NativeH3Binding(asset_id, kind, label.label, ordinal, native_input, native_slot)


def _reference_bindings(registry: ReferenceRegistry) -> tuple[NativeH3Binding, ...]:
    labels = {item.asset_id: item for item in registry.labels}
    video_labels = {item.asset_id: item for item in registry.labels if item.kind.value == "video"}
    bindings: list[NativeH3Binding] = []
    for asset in registry.assets:
        label = labels[asset.asset_id]
        if asset.kind is MediaKind.IMAGE:
            bindings.append(
                _binding_for_label(
                    asset.asset_id,
                    asset.kind,
                    label,
                    "ref_images",
                    f"ref_image_{label.ordinal - 1}",
                )
            )
        elif asset.kind is MediaKind.VIDEO:
            bindings.append(
                _binding_for_label(
                    asset.asset_id,
                    asset.kind,
                    label,
                    "ref_videos",
                    f"ref_video_{label.ordinal - 1}",
                )
            )
        elif asset.paired_video_id is not None:
            target = video_labels.get(asset.paired_video_id)
            if target is None:
                raise NativeH3AdapterError(
                    "paired_audio_target_missing", "paired soundtrack target is missing"
                )
            bindings.append(
                _binding_for_label(
                    asset.asset_id,
                    asset.kind,
                    label,
                    "ref_video_audios",
                    f"ref_video_audio_{target.ordinal - 1}",
                )
            )
        else:
            bindings.append(
                _binding_for_label(
                    asset.asset_id,
                    asset.kind,
                    label,
                    "ref_audios",
                    f"ref_audio_{label.ordinal - 1}",
                )
            )
    return tuple(bindings)


def _keyframe_bindings(registry: ReferenceRegistry, mode: TaskMode) -> tuple[NativeH3Binding, ...]:
    if not registry.assets:
        raise NativeH3AdapterError(
            "missing_keyframe", f"{mode.value} requires explicit keyframe roles"
        )
    expected_roles = {
        TaskMode.I2VA: {AssetRole.FIRST_FRAME},
        TaskMode.L2VA: {AssetRole.LAST_FRAME},
        TaskMode.FL2VA: {AssetRole.FIRST_FRAME, AssetRole.LAST_FRAME},
    }.get(mode)
    if expected_roles is None:
        raise NativeH3AdapterError(
            "unsupported_native_task_mode", "native keyframe mode is unsupported"
        )
    bindings: list[NativeH3Binding] = []
    seen_roles: set[AssetRole] = set()
    for asset in registry.assets:
        if asset.kind is not MediaKind.IMAGE or asset.role not in {
            AssetRole.FIRST_FRAME,
            AssetRole.LAST_FRAME,
        }:
            raise NativeH3AdapterError(
                "invalid_keyframe_roles",
                f"{mode.value} accepts only explicit image keyframe roles",
            )
        if asset.role in seen_roles:
            raise NativeH3AdapterError(
                "duplicate_keyframe_role", "fl2va keyframe roles must be unique"
            )
        seen_roles.add(asset.role)
        label = registry.label_for(asset.asset_id)
        input_name = "first_frame" if asset.role is AssetRole.FIRST_FRAME else "last_frame"
        bindings.append(
            _binding_for_label(asset.asset_id, asset.kind, label, input_name, input_name)
        )
    if seen_roles != expected_roles:
        raise NativeH3AdapterError(
            "missing_keyframe", f"{mode.value} keyframe roles do not match the native task"
        )
    return tuple(bindings)


def _declared_output_duration_seconds(report: ContextReport) -> Decimal:
    request = report.request
    if request.requested_duration_seconds is not None:
        return Decimal(str(request.requested_duration_seconds))
    return Decimal(str(request.effective_duration_seconds))


def _h3_base_preflight(report: ContextReport) -> tuple[str, ...]:
    """Validate official H3-Base limits without probing or opening media."""

    duration = _declared_output_duration_seconds(report)
    if not NATIVE_H3_BASE_MIN_OUTPUT_SECONDS <= duration <= (NATIVE_H3_BASE_MAX_OUTPUT_SECONDS):
        raise NativeH3AdapterError(
            "h3_base_output_duration_limit",
            "native H3 output duration must remain between 4 and 15 seconds",
        )

    assets = report.request.reference_registry.assets
    if len(assets) > NATIVE_H3_BASE_MAX_REFERENCES:
        raise NativeH3AdapterError(
            "h3_base_total_reference_limit",
            "native H3 accepts at most twelve reference files",
        )
    counts = {
        MediaKind.IMAGE: sum(asset.kind is MediaKind.IMAGE for asset in assets),
        MediaKind.VIDEO: sum(asset.kind is MediaKind.VIDEO for asset in assets),
        MediaKind.AUDIO: sum(asset.kind is MediaKind.AUDIO for asset in assets),
    }
    for kind, maximum in (
        (MediaKind.IMAGE, NATIVE_H3_BASE_MAX_IMAGES),
        (MediaKind.VIDEO, NATIVE_H3_BASE_MAX_VIDEOS),
        (MediaKind.AUDIO, NATIVE_H3_BASE_MAX_AUDIO),
    ):
        if counts[kind] > maximum:
            raise NativeH3AdapterError(
                f"h3_base_{kind.value}_reference_limit",
                f"native H3 {kind.value} reference count exceeds the official limit",
            )

    totals = {MediaKind.VIDEO: Decimal("0"), MediaKind.AUDIO: Decimal("0")}
    missing_duration = False
    for asset in assets:
        if asset.kind not in totals:
            continue
        metadata = asset.metadata
        if metadata is None or metadata.duration_seconds is None:
            missing_duration = True
            continue
        reference_duration = metadata.duration_seconds
        if (
            not NATIVE_H3_BASE_MIN_TIMED_REFERENCE_SECONDS
            <= reference_duration
            <= (NATIVE_H3_BASE_MAX_TIMED_REFERENCE_SECONDS)
        ):
            raise NativeH3AdapterError(
                "h3_base_reference_duration_limit",
                "native H3 timed references must remain between 2 and 15 seconds",
            )
        totals[asset.kind] += reference_duration
    if totals[MediaKind.VIDEO] > NATIVE_H3_BASE_MAX_VIDEO_TOTAL_SECONDS:
        raise NativeH3AdapterError(
            "h3_base_video_duration_total",
            "native H3 reference video duration exceeds fifteen seconds",
        )
    if totals[MediaKind.AUDIO] > NATIVE_H3_BASE_MAX_AUDIO_TOTAL_SECONDS:
        raise NativeH3AdapterError(
            "h3_base_audio_duration_total",
            "native H3 reference audio duration exceeds fifteen seconds",
        )
    return (NATIVE_H3_DURATION_UNVERIFIED,) if missing_duration else ()


_TRUSTED_WIRING: dict[int, tuple[weakref.ReferenceType[NativeH3Wiring], ContextReport]] = {}


def _trust_native_h3_wiring(wiring: NativeH3Wiring, report: ContextReport) -> NativeH3Wiring:
    identity = id(wiring)

    def remove(reference: weakref.ReferenceType[NativeH3Wiring]) -> None:
        current = _TRUSTED_WIRING.get(identity)
        if current is not None and current[0] is reference:
            _TRUSTED_WIRING.pop(identity, None)

    reference = weakref.ref(wiring, remove)
    _TRUSTED_WIRING[identity] = (reference, report)
    return wiring


def assert_native_h3_wiring_authority(value: object, report: ContextReport) -> NativeH3Wiring:
    """Require the exact wiring instance issued for the exact validated report."""

    if type(value) is not NativeH3Wiring or type(report) is not ContextReport:
        raise NativeH3AdapterError(
            "invalid_wiring_authority", "native H3 wiring authority requires exact values"
        )
    authority = _TRUSTED_WIRING.get(id(value))
    if authority is None or authority[0]() is not value or authority[1] is not report:
        raise NativeH3AdapterError(
            "invalid_wiring_authority",
            "native H3 wiring was not issued for this exact validated report",
        )
    return value


def build_native_h3_wiring(
    report: ContextReport | ValidatedReportEnvelope,
    *,
    expected_revision: int | None = None,
    expected_report_fingerprint: str | None = None,
) -> NativeH3Wiring:
    """Build a deterministic native-H3 manifest without touching original media values."""

    report = _validate_report(
        report,
        expected_revision=expected_revision,
        expected_report_fingerprint=expected_report_fingerprint,
    )
    request = report.request
    mode = request.task_mode
    registry = request.reference_registry
    limitations = list(_h3_base_preflight(report))
    native_inputs: tuple[str, ...]
    if mode is TaskMode.T2VA:
        if registry.assets:
            raise NativeH3AdapterError(
                "unexpected_reference_media", "t2va native conditioning has no reference slots"
            )
        native_node_id = NATIVE_H3_IMAGE_NODE_ID
        native_inputs = _NATIVE_IMAGE_INPUTS
        bindings: tuple[NativeH3Binding, ...] = ()
    elif mode in {TaskMode.I2VA, TaskMode.FL2VA, TaskMode.L2VA}:
        native_node_id = NATIVE_H3_IMAGE_NODE_ID
        native_inputs = _NATIVE_IMAGE_INPUTS
        bindings = _keyframe_bindings(registry, mode)
    elif mode is TaskMode.REF2VA:
        # IMPORTANT: REF2VA may be audio-only, but it still requires an ordered reference;
        # restoring a visual-only predicate rejects the pinned native ref_audios input.
        if not registry.assets:
            raise NativeH3AdapterError(
                "missing_reference_media",
                "ref2va native generation requires at least one reference asset",
            )
        native_node_id = NATIVE_H3_REFERENCE_NODE_ID
        native_inputs = _NATIVE_REFERENCE_INPUTS
        bindings = _reference_bindings(registry)
    else:
        raise NativeH3AdapterError(
            "unsupported_native_task_mode", "pinned native H3 has no adapter mapping for this mode"
        )

    try:
        wiring = NativeH3Wiring(
            report_id=report.report_id,
            host_version=NATIVE_H3_HOST_VERSION,
            host_revision=NATIVE_H3_HOST_REVISION,
            native_source=NATIVE_H3_SOURCE,
            native_source_blob=NATIVE_H3_SOURCE_BLOB,
            native_node_id=native_node_id,
            task_mode=mode,
            profile=request.profile,
            prompt=report.prompt_document.text,
            prompt_fingerprint=canonical_fingerprint(report.prompt_document.text),
            native_length_frames=request.effective_frame_count,
            native_input_names=native_inputs,
            bindings=bindings,
            validation_status=report.validation.status,
            limitations=tuple(limitations),
        )
        return _trust_native_h3_wiring(wiring, report)
    except NativeH3AdapterError:
        raise
    except (ContextReportError, TypeError, ValueError) as exc:
        raise NativeH3AdapterError(
            "wiring_contract_error", "native H3 wiring construction failed closed"
        ) from exc


__all__ = [
    "NATIVE_H3_HOST_REVISION",
    "NATIVE_H3_HOST_VERSION",
    "NATIVE_H3_IMAGE_NODE_ID",
    "NATIVE_H3_BASE_MAX_REFERENCES",
    "NATIVE_H3_BASE_MAX_IMAGES",
    "NATIVE_H3_BASE_MAX_VIDEOS",
    "NATIVE_H3_BASE_MAX_AUDIO",
    "NATIVE_H3_DURATION_UNVERIFIED",
    "NATIVE_H3_MAX_BINDINGS",
    "NATIVE_H3_REFERENCE_NODE_ID",
    "NATIVE_H3_SOURCE",
    "NATIVE_H3_SOURCE_BLOB",
    "NATIVE_H3_WIRING_SCHEMA",
    "NativeH3Binding",
    "NativeH3Wiring",
    "assert_native_h3_wiring_authority",
    "build_native_h3_wiring",
]
