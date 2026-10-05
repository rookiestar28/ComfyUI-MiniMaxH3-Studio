"""Closed, provider-free qualification contract for the five native H3 task modes.

The matrix freezes structural host and wiring facts only. It never imports ComfyUI, opens media,
loads weights, calls a provider, or promotes the manual-only product scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import cast

from .assisted_authoring_scope import AssistedAuthoringState
from .canonical import binary64_token, canonical_fingerprint, fingerprint_context_report
from .context_reporting import ContextReport
from .contracts import PromptProfile, TaskMode
from .errors import NativeH3AdapterError, ReportLifecycleError
from .native_composition import NativeCompositionQualification, is_audio_only_composition
from .native_h3 import (
    NATIVE_H3_HOST_REVISION,
    NATIVE_H3_HOST_VERSION,
    NATIVE_H3_IMAGE_NODE_ID,
    NATIVE_H3_REFERENCE_NODE_ID,
    NATIVE_H3_SOURCE,
    NATIVE_H3_SOURCE_BLOB,
    NativeH3Wiring,
    assert_native_h3_wiring_authority,
)
from .prompt_model_provider import load_prompt_model_catalog
from .validation_lifecycle import ValidatedReportEnvelope, require_execution_ready

NATIVE_MODE_MATRIX_SCHEMA = "h3.native_mode_matrix.v1"
NATIVE_MODE_MATRIX_VERSION = "1.0.0"
MAX_NATIVE_MODE_MATRIX_BYTES = 65_536
MAX_NATIVE_MODE_MATRIX_STRING = 512

_IMAGE_REQUIRED_INPUTS = (
    ("clip", "CLIP", None, None, None, None, ()),
    ("vae", "VAE", None, None, None, None, ()),
    ("prompt", "STRING", None, None, None, None, ()),
    ("width", "INT", 1344, 32, "MAX_RESOLUTION", 32, ()),
    ("height", "INT", 768, 32, "MAX_RESOLUTION", 32, ()),
    ("length", "INT", 124, 5, 3600, 17, ()),
)
_IMAGE_OPTIONAL_INPUTS = (
    ("first_frame", "IMAGE", None, None, None, None, ()),
    ("last_frame", "IMAGE", None, None, None, None, ()),
)
_REFERENCE_REQUIRED_INPUTS = (
    ("clip", "CLIP", None, None, None, None, ()),
    ("vae", "VAE", None, None, None, None, ()),
    ("audio_vae", "VAE", None, None, None, None, ()),
    ("prompt", "STRING", None, None, None, None, ()),
    ("width", "INT", 1344, 32, "MAX_RESOLUTION", 32, ()),
    ("height", "INT", 768, 32, "MAX_RESOLUTION", 32, ()),
    ("length", "INT", 124, 5, 3600, 17, ()),
    ("ref_image_size", "COMBO", "match", None, None, None, ("match", "max")),
)
_REFERENCE_OPTIONAL_INPUTS = (
    ("ref_images", "COMFY_AUTOGROW_V3", None, 0, 9, None, ("IMAGE", "ref_image_")),
    ("ref_videos", "COMFY_AUTOGROW_V3", None, 0, 3, None, ("IMAGE", "ref_video_")),
    (
        "ref_video_audios",
        "COMFY_AUTOGROW_V3",
        None,
        0,
        3,
        None,
        ("AUDIO", "ref_video_audio_"),
    ),
    ("ref_audios", "COMFY_AUTOGROW_V3", None, 0, 3, None, ("AUDIO", "ref_audio_")),
)
_READINESS = "current_passed_report_and_trusted_wiring"
_COMMON_LIMITATIONS = (
    "no_assisted_profile_qualification",
    "no_model_output_quality_claim",
    "no_weight_backed_execution",
)
_SURFACES = (
    "direct_node",
    "fixed_app_mode",
    "fixed_subgraph",
    "sidebar_normal_queue",
    "api_headless",
)
_RUNTIME_SURFACES = (
    "direct_node",
    "sidebar_normal_queue",
    "api_headless",
)
_SURFACE_ARTIFACT_KINDS = {
    "direct_node": "product_shell_projection",
    "fixed_app_mode": "comfyui_api_prompt",
    "fixed_subgraph": "comfyui_subgraph_definition",
    "sidebar_normal_queue": "sidebar_workspace_projection",
    "api_headless": "product_shell_node_result",
}
_GRAPH_BINDING_FIELDS = (
    "asset_id",
    "kind",
    "presentation_label",
    "presentation_ordinal",
    "source_node",
    "source_output",
    "native_node",
    "native_child_path",
    "connection_order",
    "ownership",
)


class NativeModeMatrixError(ValueError):
    """Raised when the mode matrix broadens claims or drifts from the exact contract."""


class NativeModeDisposition(str, Enum):
    """Closed qualification outcomes available to one H3 task mode."""

    STRUCTURALLY_QUALIFIED_NATIVE = "STRUCTURALLY_QUALIFIED_NATIVE"
    DETERMINISTIC_MANUAL_ONLY = "DETERMINISTIC_MANUAL_ONLY"
    UNSUPPORTED = "UNSUPPORTED"


_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class NativeGraphEdge:
    """One visible host graph edge joined to one runtime wiring binding."""

    asset_id: str
    kind: str
    presentation_label: str
    presentation_ordinal: int
    source_node: str
    source_output: int
    native_node: str
    native_child_path: str
    connection_order: int
    ownership: str

    def __post_init__(self) -> None:
        if type(self) is not NativeGraphEdge:
            raise NativeModeMatrixError("graph edge must be an exact contract value")
        for value, field in (
            (self.asset_id, "edge asset ID"),
            (self.kind, "edge kind"),
            (self.presentation_label, "edge presentation label"),
            (self.source_node, "edge source node"),
            (self.native_node, "edge native node"),
            (self.native_child_path, "edge native child path"),
            (self.ownership, "edge ownership"),
        ):
            _exact_text(value, field)
        if self.kind not in {"image", "video", "audio"}:
            raise NativeModeMatrixError("graph edge kind is unsupported")
        if type(self.presentation_ordinal) is not int or not 1 <= self.presentation_ordinal <= 256:
            raise NativeModeMatrixError("graph edge presentation ordinal is invalid")
        if type(self.source_output) is not int or not 0 <= self.source_output <= 64:
            raise NativeModeMatrixError("graph edge source output is invalid")
        if type(self.connection_order) is not int or not 0 <= self.connection_order <= 31:
            raise NativeModeMatrixError("graph edge connection order is invalid")
        if self.ownership != "direct_visible_host_edge":
            raise NativeModeMatrixError("graph edge ownership must remain direct and visible")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "presentation_label": self.presentation_label,
            "presentation_ordinal": self.presentation_ordinal,
            "source_node": self.source_node,
            "source_output": self.source_output,
            "native_node": self.native_node,
            "native_child_path": self.native_child_path,
            "connection_order": self.connection_order,
            "ownership": self.ownership,
        }


@dataclass(frozen=True, slots=True)
class NativeGraphSourceAuthority:
    """Independent visible-graph association between one asset and its source endpoint."""

    asset_id: str
    source_node: str
    source_output: int
    connection_order: int

    def __post_init__(self) -> None:
        if type(self) is not NativeGraphSourceAuthority:
            raise NativeModeMatrixError("graph source authority must be an exact contract value")
        _exact_text(self.asset_id, "source authority asset ID")
        _exact_text(self.source_node, "source authority node")
        if type(self.source_output) is not int or not 0 <= self.source_output <= 64:
            raise NativeModeMatrixError("source authority output is invalid")
        if type(self.connection_order) is not int or not 0 <= self.connection_order <= 31:
            raise NativeModeMatrixError("source authority connection order is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_node": self.source_node,
            "source_output": self.source_output,
            "connection_order": self.connection_order,
        }


@dataclass(frozen=True, slots=True)
class NativeSurfaceArtifact:
    """Exact provenance and identity projection from one real public entry artifact or adapter."""

    surface: str
    artifact_kind: str
    artifact_id: str
    artifact_fingerprint: str
    report_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    wiring_fingerprint: str
    graph_fingerprint: str
    source_authority: tuple[NativeGraphSourceAuthority, ...]
    dynamic_reference_parent_visible: bool
    schema: str = "h3.native_surface_artifact.v1"

    def __post_init__(self) -> None:
        if type(self) is not NativeSurfaceArtifact:
            raise NativeModeMatrixError("surface artifact must be an exact contract value")
        if self.schema != "h3.native_surface_artifact.v1":
            raise NativeModeMatrixError("surface artifact schema drifted")
        if (
            type(self.surface) is not str
            or self.surface not in _SURFACE_ARTIFACT_KINDS
            or self.artifact_kind != _SURFACE_ARTIFACT_KINDS[self.surface]
        ):
            raise NativeModeMatrixError("surface artifact kind does not match its public path")
        for value, field in (
            (self.artifact_id, "surface artifact ID"),
            (self.report_id, "surface artifact report ID"),
        ):
            _exact_text(value, field)
        if type(self.report_revision) is not int or not 0 <= self.report_revision <= 1_000_000:
            raise NativeModeMatrixError("surface artifact report revision is invalid")
        for value, field in (
            (self.artifact_fingerprint, "surface artifact fingerprint"),
            (self.report_fingerprint, "surface artifact report fingerprint"),
            (self.prompt_fingerprint, "surface artifact prompt fingerprint"),
            (self.wiring_fingerprint, "surface artifact wiring fingerprint"),
            (self.graph_fingerprint, "surface artifact graph fingerprint"),
        ):
            if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
                raise NativeModeMatrixError(f"{field} is malformed")
        if type(self.source_authority) is not tuple or not all(
            type(value) is NativeGraphSourceAuthority for value in self.source_authority
        ):
            raise NativeModeMatrixError("surface source authority must be an exact tuple")
        if len(self.source_authority) > 32:
            raise NativeModeMatrixError("surface source authority exceeds its bound")
        source_assets = tuple(value.asset_id for value in self.source_authority)
        source_endpoints = tuple(
            (value.source_node, value.source_output) for value in self.source_authority
        )
        source_orders = tuple(value.connection_order for value in self.source_authority)
        if (
            len(source_assets) != len(set(source_assets))
            or len(source_endpoints) != len(set(source_endpoints))
            or source_orders != tuple(range(len(self.source_authority)))
        ):
            raise NativeModeMatrixError("surface source authority is ambiguous or reordered")
        if type(self.dynamic_reference_parent_visible) is not bool:
            raise NativeModeMatrixError("surface dynamic-reference authority must be exact bool")

    @property
    def source_authority_fingerprint(self) -> str:
        return canonical_fingerprint([value.to_wire() for value in self.source_authority])

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "surface": self.surface,
            "artifact_kind": self.artifact_kind,
            "artifact_id": self.artifact_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "report_id": self.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "wiring_fingerprint": self.wiring_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "source_authority": [value.to_wire() for value in self.source_authority],
            "source_authority_fingerprint": self.source_authority_fingerprint,
            "dynamic_reference_parent_visible": self.dynamic_reference_parent_visible,
        }


@dataclass(frozen=True, slots=True)
class NativeSurfaceQualification:
    """Content-free identity join for one admitted product surface."""

    surface: str
    artifact_kind: str
    artifact_id: str
    artifact_fingerprint: str
    task_mode: TaskMode
    disposition: NativeModeDisposition
    report_id: str
    report_revision: int
    report_fingerprint: str
    prompt_fingerprint: str
    wiring_fingerprint: str
    graph_fingerprint: str
    source_authority_fingerprint: str
    identity_fingerprint: str
    queue_ready: bool
    graph_manifest_verified: bool
    dynamic_reference_parent_visible: bool
    schema: str = "h3.native_surface_qualification.v1"

    def __post_init__(self) -> None:
        if type(self) is not NativeSurfaceQualification:
            raise NativeModeMatrixError("surface qualification must be an exact contract value")
        if self.schema != "h3.native_surface_qualification.v1" or self.surface not in _SURFACES:
            raise NativeModeMatrixError("surface qualification surface/schema drifted")
        if self.artifact_kind != _SURFACE_ARTIFACT_KINDS[self.surface]:
            raise NativeModeMatrixError("surface qualification artifact kind drifted")
        _exact_text(self.artifact_id, "surface qualification artifact ID")
        if (
            type(self.task_mode) is not TaskMode
            or type(self.disposition) is not NativeModeDisposition
        ):
            raise NativeModeMatrixError("surface qualification mode/disposition is invalid")
        _exact_text(self.report_id, "surface report ID")
        if type(self.report_revision) is not int or not 0 <= self.report_revision <= 1_000_000:
            raise NativeModeMatrixError("surface report revision is invalid")
        for value, field in (
            (self.artifact_fingerprint, "surface artifact fingerprint"),
            (self.report_fingerprint, "surface report fingerprint"),
            (self.prompt_fingerprint, "surface prompt fingerprint"),
            (self.wiring_fingerprint, "surface wiring fingerprint"),
            (self.graph_fingerprint, "surface graph fingerprint"),
            (self.source_authority_fingerprint, "surface source authority fingerprint"),
            (self.identity_fingerprint, "surface identity fingerprint"),
        ):
            if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
                raise NativeModeMatrixError(f"{field} is malformed")
        if self.queue_ready is not True or self.graph_manifest_verified is not True:
            raise NativeModeMatrixError("surface qualification is not execution-ready")
        if type(self.dynamic_reference_parent_visible) is not bool:
            raise NativeModeMatrixError("surface dynamic-reference boundary is invalid")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "surface": self.surface,
            "artifact_kind": self.artifact_kind,
            "artifact_id": self.artifact_id,
            "artifact_fingerprint": self.artifact_fingerprint,
            "task_mode": self.task_mode.value,
            "disposition": self.disposition.value,
            "report_id": self.report_id,
            "report_revision": self.report_revision,
            "report_fingerprint": self.report_fingerprint,
            "prompt_fingerprint": self.prompt_fingerprint,
            "wiring_fingerprint": self.wiring_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "source_authority_fingerprint": self.source_authority_fingerprint,
            "identity_fingerprint": self.identity_fingerprint,
            "queue_ready": self.queue_ready,
            "graph_manifest_verified": self.graph_manifest_verified,
            "dynamic_reference_parent_visible": self.dynamic_reference_parent_visible,
        }


def qualify_native_mode_surface(
    value: ContextReport | ValidatedReportEnvelope,
    wiring: NativeH3Wiring,
    edges: tuple[NativeGraphEdge, ...],
    *,
    artifact: NativeSurfaceArtifact,
    composition: NativeCompositionQualification | None = None,
    expected_revision: int | None = None,
    expected_report_fingerprint: str | None = None,
) -> NativeSurfaceQualification:
    """Join a current report/wiring/graph to one independently produced surface artifact."""

    if type(artifact) is not NativeSurfaceArtifact:
        raise NativeModeMatrixError("native product surface requires exact artifact authority")
    surface = artifact.surface
    try:
        report = require_execution_ready(
            value,
            expected_revision=expected_revision,
            expected_report_fingerprint=expected_report_fingerprint,
        )
    except ReportLifecycleError as exc:
        raise NativeModeMatrixError(
            "native surface requires the exact current passed report"
        ) from exc
    if type(wiring) is not NativeH3Wiring:
        raise NativeModeMatrixError("native surface requires exact runtime-issued wiring")
    try:
        # CRITICAL: structural equality cannot replace the wiring instance issued for this report.
        assert_native_h3_wiring_authority(wiring, report)
    except NativeH3AdapterError as exc:
        raise NativeModeMatrixError(
            "native surface requires runtime-issued wiring owned by the exact report"
        ) from exc
    if not wiring.queue_ready:
        raise NativeModeMatrixError("native surface wiring is not queue-ready")
    # IMPORTANT: the historical Ref2VA surface qualifies a visual subject only. A newly
    # supported audio composition must join current host and decoded-media evidence first.
    if composition is not None:
        NativeCompositionQualification.assert_current(composition, report, wiring)
    if (is_audio_only_composition(wiring) and composition is None) or (
        composition is not None and not composition.qualified
    ):
        raise NativeModeMatrixError("native_audio_composition_unqualified")
    matrix = build_default_native_mode_matrix()
    entry = next(
        (
            candidate
            for candidate in matrix.modes
            if candidate.task_mode is report.request.task_mode
        ),
        None,
    )
    if entry is None:
        raise NativeModeMatrixError("native surface task mode is absent from the matrix")
    if surface not in entry.qualified_surfaces:
        raise NativeModeMatrixError("native product surface is unsupported for this task mode")
    expected_inputs = tuple(item.name for item in (*entry.required_inputs, *entry.optional_inputs))
    if (
        wiring.task_mode is not entry.task_mode
        or wiring.profile.name is not entry.prompt_profile
        or wiring.native_node_id != entry.native_node_id
        or wiring.native_input_names != expected_inputs
        or wiring.prompt_fingerprint != canonical_fingerprint(report.prompt_document.text)
    ):
        raise NativeModeMatrixError("native surface wiring drifted from the mode matrix")
    is_reference = entry.task_mode is TaskMode.REF2VA
    if is_reference and not artifact.dynamic_reference_parent_visible:
        raise NativeModeMatrixError("Ref2VA dynamic Reference node must remain parent-visible")
    if not is_reference and artifact.dynamic_reference_parent_visible:
        raise NativeModeMatrixError("non-reference mode cannot claim a dynamic parent boundary")
    if type(edges) is not tuple or not all(type(edge) is NativeGraphEdge for edge in edges):
        raise NativeModeMatrixError("native graph edges must be an exact tuple")
    if len(edges) != len(wiring.bindings):
        raise NativeModeMatrixError("native graph edge omission or surplus drifted from wiring")
    report_fingerprint = fingerprint_context_report(report)
    wiring_fingerprint = canonical_fingerprint(wiring.to_wire())
    graph_fingerprint = canonical_fingerprint([edge.to_wire() for edge in edges])
    if (
        artifact.report_id != report.report_id
        or artifact.report_revision != report.revision
        or artifact.report_fingerprint != report_fingerprint
        or artifact.prompt_fingerprint != wiring.prompt_fingerprint
        or artifact.wiring_fingerprint != wiring_fingerprint
    ):
        raise NativeModeMatrixError("surface artifact identity drifted from report/wiring")
    if len(artifact.source_authority) != len(edges):
        raise NativeModeMatrixError("surface source authority count drifted from graph")
    seen_sources: set[tuple[str, int]] = set()
    for index, (edge, binding, source_authority) in enumerate(
        zip(edges, wiring.bindings, artifact.source_authority, strict=True)
    ):
        source = (edge.source_node, edge.source_output)
        expected_path = f"{wiring.native_node_id}.{binding.native_path}"
        if source in seen_sources:
            raise NativeModeMatrixError("native graph edge source is duplicated")
        seen_sources.add(source)
        if (
            source_authority.asset_id != binding.asset_id
            or source_authority.source_node != edge.source_node
            or source_authority.source_output != edge.source_output
            or source_authority.connection_order != index
        ):
            raise NativeModeMatrixError("native graph source authority association drifted")
        if (
            edge.asset_id != binding.asset_id
            or edge.kind != binding.kind.value
            or edge.presentation_label != binding.label
            or edge.presentation_ordinal != binding.ordinal
            or edge.native_node != wiring.native_node_id
            or edge.native_child_path != expected_path
            or edge.connection_order != index
            or edge.ownership != "direct_visible_host_edge"
        ):
            raise NativeModeMatrixError(
                "native graph edge identity/order/ownership drifted from wiring"
            )
    if artifact.graph_fingerprint != graph_fingerprint:
        raise NativeModeMatrixError("surface artifact graph fingerprint drifted")
    identity_fingerprint = canonical_fingerprint(
        {
            **(
                {"composition_fingerprint": canonical_fingerprint(composition.to_wire())}
                if composition is not None
                else {}
            ),
            "matrix_fingerprint": matrix.fingerprint,
            "task_mode": entry.task_mode.value,
            "disposition": entry.disposition.value,
            "report_id": report.report_id,
            "report_revision": report.revision,
            "report_fingerprint": report_fingerprint,
            "prompt_fingerprint": wiring.prompt_fingerprint,
            "wiring_fingerprint": wiring_fingerprint,
            "graph_fingerprint": graph_fingerprint,
            "dynamic_reference_parent_visible": artifact.dynamic_reference_parent_visible,
        }
    )
    return NativeSurfaceQualification(
        surface=surface,
        artifact_kind=artifact.artifact_kind,
        artifact_id=artifact.artifact_id,
        artifact_fingerprint=artifact.artifact_fingerprint,
        task_mode=entry.task_mode,
        disposition=entry.disposition,
        report_id=report.report_id,
        report_revision=report.revision,
        report_fingerprint=report_fingerprint,
        prompt_fingerprint=wiring.prompt_fingerprint,
        wiring_fingerprint=wiring_fingerprint,
        graph_fingerprint=graph_fingerprint,
        source_authority_fingerprint=artifact.source_authority_fingerprint,
        identity_fingerprint=identity_fingerprint,
        queue_ready=True,
        graph_manifest_verified=True,
        dynamic_reference_parent_visible=artifact.dynamic_reference_parent_visible,
    )


def assert_native_surface_equivalence(
    values: tuple[NativeSurfaceQualification, ...],
) -> str:
    """Require every closed product surface to retain one exact execution identity."""

    if (
        type(values) is not tuple
        or not values
        or not all(type(value) is NativeSurfaceQualification for value in values)
    ):
        raise NativeModeMatrixError("surface equivalence requires exact qualifications")
    task_modes = {value.task_mode for value in values}
    if len(task_modes) != 1:
        raise NativeModeMatrixError("surface equivalence cannot mix task modes")
    task_mode = next(iter(task_modes))
    entry = next(
        value for value in build_default_native_mode_matrix().modes if value.task_mode is task_mode
    )
    if len(values) != len(entry.qualified_surfaces) or {value.surface for value in values} != set(
        entry.qualified_surfaces
    ):
        raise NativeModeMatrixError("surface equivalence inventory is incomplete or duplicated")
    if len({value.artifact_id for value in values}) != len(values) or len(
        {value.artifact_fingerprint for value in values}
    ) != len(values):
        raise NativeModeMatrixError("surface equivalence reuses one artifact provenance")
    identities = {value.identity_fingerprint for value in values}
    if len(identities) != 1:
        raise NativeModeMatrixError("product surfaces do not retain the same native identity")
    return values[0].identity_fingerprint


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_NATIVE_MODE_MATRIX_STRING:
        raise NativeModeMatrixError(f"{field} must be exact bounded text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise NativeModeMatrixError(f"{field} contains a control character")
    return value


@dataclass(frozen=True, slots=True)
class NativeSocketSpec:
    """One exact native host input descriptor without a runtime value."""

    name: str
    socket_type: str
    default: str | int | float | None = None
    minimum: str | int | float | None = None
    maximum: str | int | float | None = None
    step: int | float | None = None
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self) is not NativeSocketSpec:
            raise NativeModeMatrixError("socket spec must be an exact contract value")
        _exact_text(self.name, "socket name")
        _exact_text(self.socket_type, "socket type")
        allowed_scalar_types = {str, int, float, type(None)}
        for value, field in (
            (self.default, "socket default"),
            (self.minimum, "socket minimum"),
            (self.maximum, "socket maximum"),
            (self.step, "socket step"),
        ):
            if type(value) not in allowed_scalar_types or type(value) is bool:
                raise NativeModeMatrixError(f"{field} is not an exact scalar")
            if type(value) is str:
                _exact_text(value, field)
        if type(self.options) is not tuple or len(self.options) > 16:
            raise NativeModeMatrixError("socket options must be a bounded tuple")
        if len(self.options) != len(set(self.options)):
            raise NativeModeMatrixError("socket options contain duplicates")
        for option in self.options:
            _exact_text(option, "socket option")

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "socket_type": self.socket_type,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "step": self.step,
            "options": list(self.options),
        }


def _socket_specs(
    values: tuple[tuple[str, str, object, object, object, object, tuple[str, ...]], ...],
) -> tuple[NativeSocketSpec, ...]:
    return tuple(
        NativeSocketSpec(
            name=name,
            socket_type=socket_type,
            default=cast(str | int | float | None, default),
            minimum=cast(str | int | float | None, minimum),
            maximum=cast(str | int | float | None, maximum),
            step=cast(int | float | None, step),
            options=options,
        )
        for name, socket_type, default, minimum, maximum, step, options in values
    )


@dataclass(frozen=True, slots=True)
class NativeSigmaShiftSpec:
    """Pinned supported sampler seam without a model value."""

    node_id: str = "MiniMaxH3SigmaShift"
    input_types: tuple[tuple[str, str], ...] = (
        ("model", "MODEL"),
        ("shift_video", "FLOAT"),
        ("shift_audio", "FLOAT"),
    )
    defaults: tuple[tuple[str, float], ...] = (
        ("shift_video", 12.0),
        ("shift_audio", 3.0),
    )
    bounds: tuple[tuple[str, float], ...] = (
        ("minimum", 0.01),
        ("maximum", 100.0),
        ("step", 0.01),
    )
    sampler: str = "ModelSamplingDiscreteFlow"
    scheduler: str = "video_discrete_flow_audio_internal_shift"

    def __post_init__(self) -> None:
        if type(self) is not NativeSigmaShiftSpec or (
            self.node_id,
            self.input_types,
            self.defaults,
            self.bounds,
            self.sampler,
            self.scheduler,
        ) != (
            "MiniMaxH3SigmaShift",
            (("model", "MODEL"), ("shift_video", "FLOAT"), ("shift_audio", "FLOAT")),
            (("shift_video", 12.0), ("shift_audio", 3.0)),
            (("minimum", 0.01), ("maximum", 100.0), ("step", 0.01)),
            "ModelSamplingDiscreteFlow",
            "video_discrete_flow_audio_internal_shift",
        ):
            raise NativeModeMatrixError("SigmaShift contract drifted")

    def to_wire(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "input_types": [list(value) for value in self.input_types],
            "defaults": [[name, value] for name, value in self.defaults],
            "bounds": [[name, value] for name, value in self.bounds],
            "sampler": self.sampler,
            "scheduler": self.scheduler,
        }


@dataclass(frozen=True, slots=True)
class NativeModeQualification:
    """Structural qualification record for one exact task mode."""

    task_mode: TaskMode
    prompt_profile: PromptProfile
    disposition: NativeModeDisposition
    native_node_id: str
    required_inputs: tuple[NativeSocketSpec, ...]
    optional_inputs: tuple[NativeSocketSpec, ...]
    asset_roles: tuple[str, ...]
    minimum_assets: int
    maximum_assets: int
    native_paths: tuple[str, ...]
    dynamic_reference_boundary: str
    qualified_surfaces: tuple[str, ...]
    unsupported_surfaces: tuple[str, ...]
    limitations: tuple[str, ...]
    prompt_socket: str = "STRING"
    readiness: str = _READINESS

    def __post_init__(self) -> None:
        if type(self) is not NativeModeQualification:
            raise NativeModeMatrixError("mode qualification must be an exact contract value")
        if type(self.task_mode) is not TaskMode:
            raise NativeModeMatrixError("task mode must be an exact enum member")
        if type(self.prompt_profile) is not PromptProfile:
            raise NativeModeMatrixError("prompt profile must be an exact enum member")
        if type(self.disposition) is not NativeModeDisposition:
            raise NativeModeMatrixError("mode disposition must be an exact enum member")
        if self.disposition is not NativeModeDisposition.STRUCTURALLY_QUALIFIED_NATIVE:
            raise NativeModeMatrixError("default matrix modes require structural qualification")
        _exact_text(self.native_node_id, "native node ID")
        if self.prompt_socket != "STRING" or self.readiness != _READINESS:
            raise NativeModeMatrixError("native prompt/readiness boundary drifted")
        for socket_values, field in (
            (self.required_inputs, "required inputs"),
            (self.optional_inputs, "optional inputs"),
        ):
            if type(socket_values) is not tuple or not all(
                type(value) is NativeSocketSpec for value in socket_values
            ):
                raise NativeModeMatrixError(f"{field} must contain exact socket specs")
            names = tuple(value.name for value in socket_values)
            if len(names) != len(set(names)):
                raise NativeModeMatrixError(f"{field} contains duplicate sockets")
        for text_values, field in (
            (self.asset_roles, "asset roles"),
            (self.native_paths, "native paths"),
            (self.qualified_surfaces, "qualified surfaces"),
            (self.unsupported_surfaces, "unsupported surfaces"),
            (self.limitations, "limitations"),
        ):
            if type(text_values) is not tuple or len(text_values) != len(set(text_values)):
                raise NativeModeMatrixError(f"{field} must be a unique tuple")
            for value in text_values:
                _exact_text(value, field)
        if (
            set(self.qualified_surfaces) & set(self.unsupported_surfaces)
            or set(self.qualified_surfaces) | set(self.unsupported_surfaces) != set(_SURFACES)
            or tuple(value for value in _SURFACES if value in self.qualified_surfaces)
            != self.qualified_surfaces
            or tuple(value for value in _SURFACES if value in self.unsupported_surfaces)
            != self.unsupported_surfaces
        ):
            raise NativeModeMatrixError("mode surface disposition is incomplete or reordered")
        if (
            type(self.minimum_assets) is not int
            or type(self.maximum_assets) is not int
            or not 0 <= self.minimum_assets <= self.maximum_assets <= 32
        ):
            raise NativeModeMatrixError("mode asset cardinality is invalid")
        _exact_text(self.dynamic_reference_boundary, "dynamic reference boundary")

    def to_wire(self) -> dict[str, object]:
        return {
            "task_mode": self.task_mode.value,
            "prompt_profile": self.prompt_profile.value,
            "disposition": self.disposition.value,
            "native_node_id": self.native_node_id,
            "prompt_socket": self.prompt_socket,
            "required_inputs": [value.to_wire() for value in self.required_inputs],
            "optional_inputs": [value.to_wire() for value in self.optional_inputs],
            "asset_roles": list(self.asset_roles),
            "minimum_assets": self.minimum_assets,
            "maximum_assets": self.maximum_assets,
            "native_paths": list(self.native_paths),
            "dynamic_reference_boundary": self.dynamic_reference_boundary,
            "qualified_surfaces": list(self.qualified_surfaces),
            "unsupported_surfaces": list(self.unsupported_surfaces),
            "readiness": self.readiness,
            "limitations": list(self.limitations),
        }


def _qualification(
    mode: TaskMode,
    profile: PromptProfile,
    node_id: str,
    required: tuple[tuple[str, str, object, object, object, object, tuple[str, ...]], ...],
    optional: tuple[tuple[str, str, object, object, object, object, tuple[str, ...]], ...],
    roles: tuple[str, ...],
    minimum: int,
    maximum: int,
    paths: tuple[str, ...],
    dynamic_boundary: str,
    qualified_surfaces: tuple[str, ...],
    limitations: tuple[str, ...] = _COMMON_LIMITATIONS,
) -> NativeModeQualification:
    return NativeModeQualification(
        task_mode=mode,
        prompt_profile=profile,
        disposition=NativeModeDisposition.STRUCTURALLY_QUALIFIED_NATIVE,
        native_node_id=node_id,
        required_inputs=_socket_specs(required),
        optional_inputs=_socket_specs(optional),
        asset_roles=roles,
        minimum_assets=minimum,
        maximum_assets=maximum,
        native_paths=paths,
        dynamic_reference_boundary=dynamic_boundary,
        qualified_surfaces=qualified_surfaces,
        unsupported_surfaces=tuple(
            surface for surface in _SURFACES if surface not in qualified_surfaces
        ),
        limitations=limitations,
    )


def _mode_inventory() -> tuple[NativeModeQualification, ...]:
    image = _IMAGE_REQUIRED_INPUTS
    image_optional = _IMAGE_OPTIONAL_INPUTS
    return (
        _qualification(
            TaskMode.T2VA,
            PromptProfile.BASE,
            NATIVE_H3_IMAGE_NODE_ID,
            image,
            image_optional,
            (),
            0,
            0,
            (),
            "not_applicable",
            _SURFACES,
        ),
        _qualification(
            TaskMode.I2VA,
            PromptProfile.BASE,
            NATIVE_H3_IMAGE_NODE_ID,
            image,
            image_optional,
            ("first_frame",),
            1,
            1,
            ("MiniMaxH3ImageToVideo.first_frame",),
            "not_applicable",
            _RUNTIME_SURFACES,
        ),
        _qualification(
            TaskMode.FL2VA,
            PromptProfile.BASE,
            NATIVE_H3_IMAGE_NODE_ID,
            image,
            image_optional,
            ("first_frame", "last_frame"),
            2,
            2,
            (
                "MiniMaxH3ImageToVideo.first_frame",
                "MiniMaxH3ImageToVideo.last_frame",
            ),
            "not_applicable",
            _RUNTIME_SURFACES,
        ),
        _qualification(
            TaskMode.L2VA,
            PromptProfile.BASE,
            NATIVE_H3_IMAGE_NODE_ID,
            image,
            image_optional,
            ("last_frame",),
            1,
            1,
            ("MiniMaxH3ImageToVideo.last_frame",),
            "not_applicable",
            _RUNTIME_SURFACES,
        ),
        _qualification(
            TaskMode.REF2VA,
            PromptProfile.FULL_REFERENCE,
            NATIVE_H3_REFERENCE_NODE_ID,
            _REFERENCE_REQUIRED_INPUTS,
            _REFERENCE_OPTIONAL_INPUTS,
            ("reference", "audio_source"),
            1,
            12,
            (
                "MiniMaxH3ReferenceToVideo.ref_images.ref_image_{index}",
                "MiniMaxH3ReferenceToVideo.ref_videos.ref_video_{index}",
                "MiniMaxH3ReferenceToVideo.ref_video_audios.ref_video_audio_{video_index}",
                "MiniMaxH3ReferenceToVideo.ref_audios.ref_audio_{index}",
            ),
            "parent_graph_visible",
            (
                "direct_node",
                "fixed_app_mode",
                "sidebar_normal_queue",
                "api_headless",
            ),
            _COMMON_LIMITATIONS
            + (
                "dynamic_reference_node_must_remain_parent_visible",
                "fixed_subgraph_does_not_claim_arbitrary_cardinality",
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class NativeModeMatrix:
    """Exact five-mode structural matrix and non-escalating product claim boundary."""

    modes: tuple[NativeModeQualification, ...]
    sigma_shift: NativeSigmaShiftSpec
    schema: str = NATIVE_MODE_MATRIX_SCHEMA
    version: str = NATIVE_MODE_MATRIX_VERSION
    product_scope: str = "MANUAL_ONLY_SCOPED"
    claim_scope: str = "structural_native_contract_only"
    host_version: str = NATIVE_H3_HOST_VERSION
    host_revision: str = NATIVE_H3_HOST_REVISION
    native_source: str = NATIVE_H3_SOURCE
    native_source_blob: str = NATIVE_H3_SOURCE_BLOB
    prompt_boundary: str = "STRING"
    presentation_label_basis: str = "one_based"
    autogrow_path_basis: str = "fully_qualified_zero_based"
    paired_audio_basis: str = "same_zero_based_video_suffix"
    surfaces: tuple[str, ...] = _SURFACES
    graph_binding_fields: tuple[str, ...] = _GRAPH_BINDING_FIELDS
    host_contract_lane: str = "real_classes_real_schema_weight_callable_stub_only"
    stripped_native_qualifies: bool = False
    model_free_qualifies: bool = False
    assisted_profiles: tuple[str, ...] = dataclass_field(
        default_factory=lambda: tuple(
            profile.profile_id for profile in load_prompt_model_catalog().profiles
        )
    )
    assisted_authoring: AssistedAuthoringState = dataclass_field(
        default_factory=lambda: AssistedAuthoringState(True, False, False, False, False)
    )
    model_executions: tuple[str, ...] = ()
    provider_executions: tuple[str, ...] = ()
    media_executions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "host_identity_does_not_establish_capability",
        "no_assisted_profile_qualification",
        "no_model_output_quality_claim",
        "no_weight_backed_execution",
    )

    def __post_init__(self) -> None:
        if type(self) is not NativeModeMatrix:
            raise NativeModeMatrixError("matrix must be an exact contract value")
        if self.schema != NATIVE_MODE_MATRIX_SCHEMA or self.version != NATIVE_MODE_MATRIX_VERSION:
            raise NativeModeMatrixError("matrix schema/version drifted")
        if self.product_scope != "MANUAL_ONLY_SCOPED":
            raise NativeModeMatrixError("matrix product scope must remain manual-only")
        if self.claim_scope != "structural_native_contract_only":
            raise NativeModeMatrixError("matrix claim scope escalated")
        if type(self.modes) is not tuple or self.modes != _mode_inventory():
            raise NativeModeMatrixError("matrix mode inventory drifted")
        if type(self.sigma_shift) is not NativeSigmaShiftSpec:
            raise NativeModeMatrixError("matrix SigmaShift contract is invalid")
        if (
            type(self.host_version) is not str
            or len(self.host_version) > 64
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?", self.host_version)
            is None
            or type(self.host_revision) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.host_revision) is None
            or type(self.native_source_blob) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.native_source_blob) is None
        ):
            raise NativeModeMatrixError("matrix host provenance is malformed")
        exact_values = (
            (self.native_source, NATIVE_H3_SOURCE, "native source"),
            (self.prompt_boundary, "STRING", "prompt boundary"),
            (self.presentation_label_basis, "one_based", "presentation label basis"),
            (self.autogrow_path_basis, "fully_qualified_zero_based", "Autogrow path basis"),
            (self.paired_audio_basis, "same_zero_based_video_suffix", "paired audio basis"),
            (self.surfaces, _SURFACES, "surface inventory"),
            (self.graph_binding_fields, _GRAPH_BINDING_FIELDS, "graph binding fields"),
            (
                self.host_contract_lane,
                "real_classes_real_schema_weight_callable_stub_only",
                "host contract lane",
            ),
            (self.stripped_native_qualifies, False, "stripped-native disposition"),
            (self.model_free_qualifies, False, "model-free disposition"),
            (
                self.assisted_profiles,
                tuple(profile.profile_id for profile in load_prompt_model_catalog().profiles),
                "assisted profiles",
            ),
            (
                self.assisted_authoring,
                AssistedAuthoringState(True, False, False, False, False),
                "assisted authoring",
            ),
            (self.model_executions, (), "model executions"),
            (self.provider_executions, (), "provider executions"),
            (self.media_executions, (), "media executions"),
        )
        for actual, expected, field in exact_values:
            if type(actual) is not type(expected) or actual != expected:
                if field == "assisted profiles":
                    raise NativeModeMatrixError("matrix assisted profiles are not authorized")
                raise NativeModeMatrixError(f"matrix {field} drifted")
        if type(self.limitations) is not tuple or self.limitations != (
            "host_identity_does_not_establish_capability",
            "no_assisted_profile_qualification",
            "no_model_output_quality_claim",
            "no_weight_backed_execution",
        ):
            raise NativeModeMatrixError("matrix limitations drifted")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(_fingerprint_projection(self.to_wire()))

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "product_scope": self.product_scope,
            "claim_scope": self.claim_scope,
            "host_version": self.host_version,
            "host_revision": self.host_revision,
            "native_source": self.native_source,
            "native_source_blob": self.native_source_blob,
            "prompt_boundary": self.prompt_boundary,
            "presentation_label_basis": self.presentation_label_basis,
            "autogrow_path_basis": self.autogrow_path_basis,
            "paired_audio_basis": self.paired_audio_basis,
            "surfaces": list(self.surfaces),
            "graph_binding_fields": list(self.graph_binding_fields),
            "host_contract_lane": self.host_contract_lane,
            "stripped_native_qualifies": self.stripped_native_qualifies,
            "model_free_qualifies": self.model_free_qualifies,
            "assisted_profiles": list(self.assisted_profiles),
            "assisted_authoring": self.assisted_authoring.to_wire(),
            "modes": [mode.to_wire() for mode in self.modes],
            "sigma_shift": self.sigma_shift.to_wire(),
            "model_executions": list(self.model_executions),
            "provider_executions": list(self.provider_executions),
            "media_executions": list(self.media_executions),
            "limitations": list(self.limitations),
        }


def build_default_native_mode_matrix() -> NativeModeMatrix:
    """Build a fresh exact matrix without consulting mutable host or provider state."""

    return NativeModeMatrix(modes=_mode_inventory(), sigma_shift=NativeSigmaShiftSpec())


def _fingerprint_projection(value: object) -> object:
    """Encode exact numeric host defaults before using the narrow project canonicalizer."""

    if type(value) is float:
        return binary64_token(value)
    if type(value) is dict:
        return {
            cast(str, key): _fingerprint_projection(item)
            for key, item in cast(dict[object, object], value).items()
        }
    if type(value) is list:
        return [_fingerprint_projection(item) for item in cast(list[object], value)]
    return value


def _reject_non_json_types(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise NativeModeMatrixError("matrix wire exceeds the depth limit")
    if value is None or type(value) in {str, int, float, bool}:
        if type(value) is str and len(value) > MAX_NATIVE_MODE_MATRIX_STRING:
            raise NativeModeMatrixError("matrix wire string exceeds the limit")
        if type(value) is float and (value != value or value in {float("inf"), float("-inf")}):
            raise NativeModeMatrixError("matrix wire contains a non-finite number")
        return
    if type(value) is list:
        if len(value) > 256:
            raise NativeModeMatrixError("matrix wire list exceeds the limit")
        for item in value:
            _reject_non_json_types(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 64:
            raise NativeModeMatrixError("matrix wire mapping exceeds the limit")
        for key, item in value.items():
            if type(key) is not str:
                raise NativeModeMatrixError("matrix wire key must be a string")
            _reject_non_json_types(item, depth=depth + 1)
        return
    raise NativeModeMatrixError("matrix wire contains a non-JSON exact type")


def validate_native_mode_matrix_wire(value: object) -> NativeModeMatrix:
    """Validate a JSON-safe value against the exact frozen five-mode matrix."""

    _reject_non_json_types(value)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise NativeModeMatrixError("matrix wire is not canonical JSON") from exc
    if len(encoded) > MAX_NATIVE_MODE_MATRIX_BYTES:
        raise NativeModeMatrixError("matrix wire exceeds the byte limit")
    expected = build_default_native_mode_matrix()
    expected_encoded = json.dumps(
        expected.to_wire(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if encoded != expected_encoded:
        raise NativeModeMatrixError("matrix wire does not match the frozen contract")
    return expected


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NativeModeMatrixError(f"matrix JSON contains duplicate member {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise NativeModeMatrixError(f"matrix JSON contains invalid constant {value!r}")


def decode_native_mode_matrix_json(payload: str | bytes | bytearray) -> NativeModeMatrix:
    """Decode strict UTF-8 JSON with exact type, duplicate, and size enforcement."""

    # CRITICAL: exact built-in types prevent hostile subclasses from spoofing byte bounds.
    if type(payload) is str:
        try:
            encoded = payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise NativeModeMatrixError("matrix JSON is not strict UTF-8") from exc
        text = payload
    elif type(payload) in {bytes, bytearray}:
        encoded = bytes(cast(bytes | bytearray, payload))
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise NativeModeMatrixError("matrix JSON is not strict UTF-8") from exc
    else:
        raise NativeModeMatrixError("matrix JSON must be text or bytes")
    if len(encoded) > MAX_NATIVE_MODE_MATRIX_BYTES:
        raise NativeModeMatrixError("matrix JSON exceeds the byte limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except NativeModeMatrixError:
        raise
    except json.JSONDecodeError as exc:
        raise NativeModeMatrixError("matrix JSON is malformed") from exc
    return validate_native_mode_matrix_wire(value)


__all__ = [
    "MAX_NATIVE_MODE_MATRIX_BYTES",
    "NATIVE_MODE_MATRIX_SCHEMA",
    "NATIVE_MODE_MATRIX_VERSION",
    "NativeGraphEdge",
    "NativeGraphSourceAuthority",
    "NativeModeDisposition",
    "NativeModeMatrix",
    "NativeModeMatrixError",
    "NativeModeQualification",
    "NativeSigmaShiftSpec",
    "NativeSocketSpec",
    "NativeSurfaceArtifact",
    "NativeSurfaceQualification",
    "assert_native_surface_equivalence",
    "build_default_native_mode_matrix",
    "decode_native_mode_matrix_json",
    "qualify_native_mode_surface",
    "validate_native_mode_matrix_wire",
]
