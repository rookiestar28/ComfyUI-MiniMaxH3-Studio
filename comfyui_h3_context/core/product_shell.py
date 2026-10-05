"""Bounded backend-owned projection for the transparent M15 product shell.

The projection joins an exact validated report, its runtime-issued native wiring, the declarative
binding manifest, the supported V1 host pair, and the accepted local-qualification disposition.
It contains no prompt text, media value, locator, provider payload, credential, or browser state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .assisted_authoring_scope import AssistedAuthoringState
from .canonical import fingerprint_context_report
from .capability_manifest import build_default_binding_manifest
from .context_reporting import ContextReport
from .errors import NativeH3AdapterError, ProductShellError
from .local_qualification import (
    ProductScopeDisposition,
    build_default_local_qualification_plan,
    evaluate_local_qualification,
)
from .native_composition import (
    NativeCompositionQualification,
    is_audio_only_composition,
    qualify_native_composition,
)
from .native_h3 import NativeH3Wiring, assert_native_h3_wiring_authority
from .prompt_model_provider import load_prompt_model_catalog
from .ui_projection import ExecutionCorrelation

PRODUCT_SHELL_SCHEMA = "h3.context.product.shell.v1"
PRODUCT_SHELL_NODE_ID = "comfyui_h3_context.H3Context.ProductShell"
PRODUCT_SHELL_MAX_BYTES = 32_768
PRODUCT_SHELL_MAX_BINDINGS = 32
PRODUCT_SHELL_MAX_FIELDS = 32
PRODUCT_SHELL_MAX_LIMITATIONS = 32
SUPPORTED_NODE_API = "V1_ONLY"
SUPPORTED_CORE_VERSION = "0.32.0"
SUPPORTED_CORE_REVISION = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"
SUPPORTED_FRONTEND_VERSION = "1.48.7"
SUPPORTED_FRONTEND_REVISION = "6d6af63c00f132cd25dc29307fc56bd2c094fa22"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_FIELD_ID = re.compile(r"h3\.[a-z0-9_.-]{1,191}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_CHILD_PATH = re.compile(
    r"(?:MiniMaxH3ImageToVideo\.(?:first_frame|last_frame)"
    r"|MiniMaxH3ReferenceToVideo\.(?:ref_images\.ref_image_(?:0|[1-9][0-9]{0,2})"
    r"|ref_videos\.ref_video_(?:0|[1-9][0-9]{0,2})"
    r"|ref_video_audios\.ref_video_audio_(?:0|[1-9][0-9]{0,2})"
    r"|ref_audios\.ref_audio_(?:0|[1-9][0-9]{0,2})))\Z"
)
_SENSITIVE = re.compile(
    r"(?i)(?:https?://|file://|authorization|bearer\s|api[_-]?key|password|secret|"
    r"token\s*=|sig\s*=|x-amz-|(?:[A-Za-z]:[\\/]|/(?:home|mnt|tmp|var|Users|private)/))"
)


def _text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProductShellError("invalid_product_shell", f"{field} must be bounded text")
    if _SENSITIVE.search(value) or any(ord(char) < 0x20 for char in value):
        raise ProductShellError("unsafe_product_shell", f"{field} contains sensitive metadata")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field, 192)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ProductShellError("invalid_product_shell", f"{field} is not an identifier")
    return text


def _fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ProductShellError("invalid_product_shell", f"{field} is not a SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class ProductShellHostProfile:
    """Observed host/frontend provenance plus the required node API capability."""

    node_api: str = SUPPORTED_NODE_API
    core_version: str = SUPPORTED_CORE_VERSION
    core_revision: str = SUPPORTED_CORE_REVISION
    frontend_version: str = SUPPORTED_FRONTEND_VERSION
    frontend_revision: str = SUPPORTED_FRONTEND_REVISION

    def __post_init__(self) -> None:
        if self.node_api != SUPPORTED_NODE_API:
            raise ProductShellError("unsupported_host", "product-shell node API is unavailable")
        if (
            not isinstance(self.core_version, str)
            or len(self.core_version) > 64
            or _VERSION.fullmatch(self.core_version) is None
            or not isinstance(self.frontend_version, str)
            or len(self.frontend_version) > 64
            or _VERSION.fullmatch(self.frontend_version) is None
            or not isinstance(self.core_revision, str)
            or not isinstance(self.frontend_revision, str)
            or _REVISION.fullmatch(self.core_revision) is None
            or _REVISION.fullmatch(self.frontend_revision) is None
        ):
            raise ProductShellError("unsupported_host", "product-shell host revision is malformed")

    def to_wire(self) -> dict[str, str]:
        return {
            "node_api": self.node_api,
            "core_version": self.core_version,
            "core_revision": self.core_revision,
            "frontend_version": self.frontend_version,
            "frontend_revision": self.frontend_revision,
        }


@dataclass(frozen=True, slots=True)
class ProductShellBinding:
    """Content-free mapping from one presentation label to one native child path."""

    asset_id: str
    kind: str
    presentation_label: str
    presentation_ordinal: int
    native_input: str
    native_child_path: str

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "binding asset_id")
        if self.kind not in {"image", "video", "audio"}:
            raise ProductShellError("invalid_binding", "binding kind is unsupported")
        if (
            isinstance(self.presentation_ordinal, bool)
            or not isinstance(self.presentation_ordinal, int)
            or not 1 <= self.presentation_ordinal <= 256
        ):
            raise ProductShellError("invalid_binding", "presentation ordinal is invalid")
        expected = {
            "image": "Picture",
            "video": "Video",
            "audio": "Audio",
        }[self.kind]
        if self.presentation_label != f"<{expected} {self.presentation_ordinal}>":
            raise ProductShellError("invalid_binding", "presentation label and ordinal drift")
        _identifier(self.native_input, "binding native_input")
        if _CHILD_PATH.fullmatch(self.native_child_path) is None:
            raise ProductShellError(
                "invalid_binding", "native child path is not the frozen zero-based V3 path"
            )

    def to_wire(self) -> dict[str, str | int]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "presentation_label": self.presentation_label,
            "presentation_ordinal": self.presentation_ordinal,
            "native_input": self.native_input,
            "native_child_path": self.native_child_path,
        }


@dataclass(frozen=True, slots=True)
class ProductShellProjection:
    """Closed product-shell payload consumed by the locally bundled frontend."""

    product_scope: ProductScopeDisposition
    qualification_plan_fingerprint: str
    report_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    correlation: ExecutionCorrelation
    task_mode: str
    profile: str
    host: ProductShellHostProfile
    native_node_id: str
    prompt_export_ready: bool
    native_queue_ready: bool
    assisted_ready: bool
    readiness_reason: str
    field_ids: tuple[str, ...]
    bindings: tuple[ProductShellBinding, ...]
    limitations: tuple[str, ...]
    assisted_authoring: AssistedAuthoringState
    schema: str = PRODUCT_SHELL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRODUCT_SHELL_SCHEMA:
            raise ProductShellError("unsupported_schema", "product-shell schema is unsupported")
        if self.product_scope is not ProductScopeDisposition.MANUAL_ONLY_SCOPED:
            raise ProductShellError(
                "unsupported_product_scope", "this release is manual-only scoped"
            )
        _fingerprint(self.qualification_plan_fingerprint, "qualification plan fingerprint")
        _identifier(self.report_id, "report_id")
        if (
            isinstance(self.report_revision, bool)
            or not isinstance(self.report_revision, int)
            or not 0 <= self.report_revision <= 1_000_000
        ):
            raise ProductShellError("invalid_revision", "report revision is out of bounds")
        _fingerprint(self.report_fingerprint, "report fingerprint")
        _fingerprint(self.prompt_fingerprint, "prompt fingerprint")
        if not isinstance(self.correlation, ExecutionCorrelation):
            raise ProductShellError("invalid_correlation", "execution correlation is invalid")
        _identifier(self.task_mode, "task_mode")
        _identifier(self.profile, "profile")
        if not isinstance(self.host, ProductShellHostProfile):
            raise ProductShellError("unsupported_host", "product-shell host profile is invalid")
        _identifier(self.native_node_id, "native_node_id")
        if (
            self.prompt_export_ready is not True
            or type(self.native_queue_ready) is not bool
            or self.assisted_ready is not False
            or self.readiness_reason
            != ("manual_only_scoped" if self.native_queue_ready else "native_input_unqualified")
        ):
            raise ProductShellError(
                "invalid_readiness", "manual-only product readiness is internally inconsistent"
            )
        if self.assisted_authoring != AssistedAuthoringState(True, False, False, False, False):
            raise ProductShellError(
                "invalid_readiness", "assisted-authoring scope contradicts product-shell authority"
            )
        if (
            not isinstance(self.field_ids, tuple)
            or not 1 <= len(self.field_ids) <= PRODUCT_SHELL_MAX_FIELDS
            or tuple(sorted(set(self.field_ids))) != self.field_ids
            or any(_FIELD_ID.fullmatch(value) is None for value in self.field_ids)
        ):
            raise ProductShellError("invalid_fields", "product-shell field inventory is invalid")
        if (
            not isinstance(self.bindings, tuple)
            or len(self.bindings) > PRODUCT_SHELL_MAX_BINDINGS
            or not all(isinstance(value, ProductShellBinding) for value in self.bindings)
        ):
            raise ProductShellError("invalid_binding", "product-shell bindings are invalid")
        if len({value.asset_id for value in self.bindings}) != len(self.bindings):
            raise ProductShellError(
                "duplicate_binding", "product-shell bindings duplicate an asset"
            )
        if (
            not isinstance(self.limitations, tuple)
            or len(self.limitations) > PRODUCT_SHELL_MAX_LIMITATIONS
            or tuple(sorted(set(self.limitations))) != self.limitations
        ):
            raise ProductShellError("invalid_limitations", "limitations are not a closed set")
        for limitation in self.limitations:
            _text(limitation, "limitation", 256)
        encoded = json.dumps(
            self.to_wire(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > PRODUCT_SHELL_MAX_BYTES:
            raise ProductShellError(
                "projection_limit", "product-shell projection exceeds its bound"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "product_scope": self.product_scope.value,
            "qualification_plan_fingerprint": self.qualification_plan_fingerprint,
            "report_id": self.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "correlation": self.correlation.to_wire(),
            "task_mode": self.task_mode,
            "profile": self.profile,
            "host": self.host.to_wire(),
            "native_node_id": self.native_node_id,
            "prompt_export_ready": self.prompt_export_ready,
            "native_queue_ready": self.native_queue_ready,
            "assisted_ready": self.assisted_ready,
            "readiness_reason": self.readiness_reason,
            "field_ids": list(self.field_ids),
            "bindings": [value.to_wire() for value in self.bindings],
            "limitations": list(self.limitations),
            "assisted_authoring": self.assisted_authoring.to_wire(),
        }

    def to_ui(self) -> dict[str, tuple[object, ...]]:
        return {key: (value,) for key, value in self.to_wire().items()}


def _field_ids() -> tuple[str, ...]:
    manifest = build_default_binding_manifest()
    selected = tuple(
        sorted(
            value.field_id
            for value in manifest.bindings
            if value.node_id
            in {PRODUCT_SHELL_NODE_ID, "comfyui_h3_context.H3Context.NativeH3Adapter"}
        )
    )
    if not selected or not any(
        PRODUCT_SHELL_NODE_ID in value.node_id for value in manifest.bindings
    ):
        raise ProductShellError(
            "missing_field_authority", "product-shell field authority is absent"
        )
    return selected


def _binding_path(wiring: NativeH3Wiring, native_input: str, native_slot: str) -> str:
    if wiring.native_node_id == "MiniMaxH3ImageToVideo":
        return f"{wiring.native_node_id}.{native_slot}"
    return f"{wiring.native_node_id}.{native_input}.{native_slot}"


def build_product_shell_projection(
    report: ContextReport,
    wiring: NativeH3Wiring,
    correlation: ExecutionCorrelation,
    *,
    composition: NativeCompositionQualification | None = None,
) -> ProductShellProjection:
    """Join exact backend authorities into the only browser-facing product projection."""

    if type(report) is not ContextReport or type(wiring) is not NativeH3Wiring:
        raise ProductShellError("invalid_authority", "product shell requires exact backend values")
    try:
        # CRITICAL: structural equality cannot replace the runtime-issued wiring/report authority.
        assert_native_h3_wiring_authority(wiring, report)
    except NativeH3AdapterError as exc:
        raise ProductShellError(
            "invalid_authority", "native wiring is not owned by this report"
        ) from exc
    if wiring.report_id != report.report_id or wiring.prompt != report.prompt_document.text:
        raise ProductShellError("stale_wiring", "native wiring does not match the exact report")
    if not isinstance(correlation, ExecutionCorrelation):
        raise ProductShellError("invalid_correlation", "product shell requires host correlation")

    plan = build_default_local_qualification_plan()
    qualification = evaluate_local_qualification(plan)
    if qualification.product_scope is not ProductScopeDisposition.MANUAL_ONLY_SCOPED:
        raise ProductShellError(
            "unsupported_product_scope", "assisted product scope is not activated"
        )
    bindings = tuple(
        ProductShellBinding(
            asset_id=value.asset_id,
            kind=value.kind.value,
            presentation_label=value.label,
            presentation_ordinal=value.ordinal,
            native_input=value.native_input,
            native_child_path=_binding_path(wiring, value.native_input, value.native_slot),
        )
        for value in wiring.bindings
    )
    if composition is not None:
        NativeCompositionQualification.assert_current(composition, report, wiring)
    elif is_audio_only_composition(wiring):
        composition = qualify_native_composition(report, wiring)
    native_ready = wiring.queue_ready and (composition is None or composition.qualified)
    composition_limits = (
        () if composition is None or composition.qualified else (composition.reason,)
    )
    limitations = tuple(
        sorted(set((*qualification.limitations, *wiring.limitations, *composition_limits)))
    )
    return ProductShellProjection(
        product_scope=qualification.product_scope,
        qualification_plan_fingerprint=plan.fingerprint,
        report_id=report.report_id,
        report_revision=report.revision,
        report_fingerprint=fingerprint_context_report(report),
        prompt_fingerprint=wiring.prompt_fingerprint,
        correlation=correlation,
        task_mode=wiring.task_mode.value,
        profile=wiring.profile.name.value,
        host=ProductShellHostProfile(),
        native_node_id=wiring.native_node_id,
        prompt_export_ready=True,
        # IMPORTANT: prompt export does not qualify native inputs. Propagate the exact
        # wiring hold or an unqualified composition becomes an optimistic Queue action.
        native_queue_ready=native_ready,
        assisted_ready=False,
        readiness_reason="manual_only_scoped" if native_ready else "native_input_unqualified",
        field_ids=_field_ids(),
        bindings=bindings,
        limitations=limitations,
        assisted_authoring=AssistedAuthoringState(
            available=bool(load_prompt_model_catalog().profiles),
            selected=False,
            ready=False,
            authorized_for_this_action=False,
            defaulted=False,
        ),
    )


def _scan_sensitive(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_sensitive(key)
            _scan_sensitive(item)
    elif isinstance(value, list):
        for item in value:
            _scan_sensitive(item)
    elif isinstance(value, str) and _SENSITIVE.search(value):
        raise ProductShellError("unsafe_product_shell", "wire contains sensitive metadata")


def validate_product_shell_wire(
    value: object,
    report: ContextReport,
    wiring: NativeH3Wiring,
    correlation: ExecutionCorrelation,
    *,
    composition: NativeCompositionQualification | None = None,
) -> ProductShellProjection:
    """Validate a portable wire against independently supplied runtime authorities."""

    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProductShellError("invalid_product_shell", "wire must be an object")
    _scan_sensitive(value)
    # CRITICAL: correlation is runtime authority; never reconstruct it from the untrusted wire.
    if not isinstance(correlation, ExecutionCorrelation):
        raise ProductShellError("invalid_correlation", "trusted runtime correlation is required")
    expected = build_product_shell_projection(report, wiring, correlation, composition=composition)
    if value != expected.to_wire():
        raise ProductShellError(
            "semantic_drift", "wire is not derived from the exact backend authorities"
        )
    return expected


__all__ = [
    "PRODUCT_SHELL_MAX_BINDINGS",
    "PRODUCT_SHELL_MAX_BYTES",
    "PRODUCT_SHELL_MAX_FIELDS",
    "PRODUCT_SHELL_MAX_LIMITATIONS",
    "PRODUCT_SHELL_NODE_ID",
    "PRODUCT_SHELL_SCHEMA",
    "SUPPORTED_CORE_REVISION",
    "SUPPORTED_CORE_VERSION",
    "SUPPORTED_FRONTEND_REVISION",
    "SUPPORTED_FRONTEND_VERSION",
    "SUPPORTED_NODE_API",
    "ProductShellBinding",
    "ProductShellHostProfile",
    "ProductShellProjection",
    "build_product_shell_projection",
    "validate_product_shell_wire",
]
