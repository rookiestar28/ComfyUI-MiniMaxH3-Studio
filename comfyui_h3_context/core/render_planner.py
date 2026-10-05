"""Pure, deterministic M25-17 render planning over accepted composition values.

The planner consumes content-free source and font facts.  It does not import adapters, inspect a
runtime, open media, discover fonts, or qualify a renderer.  M25-18 must revalidate the carried
currentness claim before reading a source and again before publishing an artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

from .canonical import canonical_bytes, canonical_fingerprint
from .composition_contract import (
    AUDIO_EXTENSION_SCHEMA,
    ENGINE_PROFILE_ID,
    IDENTITY_CLIP_AUDIO,
    INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    MAX_ASSETS,
    MAX_EXTENT_FRAMES,
    NLE_OPERATION_IDS,
    OUTPUT_PROFILE_ID,
    ClipAudio,
    CompositionContractError,
    EmbeddedAudioSpan,
    OutputProfile,
    PublicCompositionSnapshot,
    ResolvedLayer,
    decode_public_snapshot,
    resolve_composition,
    resolve_source_interval_coverage,
)
from .errors import CanonicalizationError, ContractValidationError

RENDER_PLAN_SCHEMA: Final = "h3.context.render_plan.v1"
RENDERER_PROFILE_SCHEMA: Final = "h3.context.renderer_capability_profile.v1"
RENDERER_PROFILE_ID: Final = "h3.render.required_backend_profile.v1"
SOURCE_CURRENTNESS_SCHEMA: Final = "h3.context.source_currentness_confirmation.v1"
FONT_FACTS_SCHEMA: Final = "h3.context.packaged_font_facts.v1"
PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA: Final = "h3.context.private_source_facts_manifest.v1"
PACKAGED_FONT_MANIFEST_SCHEMA: Final = "h3.authoring.packaged_font_manifest.v1"
PACKAGED_FONT_PROFILE_ID: Final = "h3.authoring.font_profile.v1"

RENDER_PRIMITIVE_IDS: Final = (
    "SelectSourceRangeV1",
    "CropV1",
    "Transform2DV1",
    "ColorAdjustV1",
    "OpacityV1",
    "BlendV1",
    "CrossDissolveV1",
    "DrawTextV1",
    "PrimaryEmbeddedFollowVideoV1",
)
RENDERER_QUALIFICATION_ROW_IDS: Final = (
    "renderer_identity",
    "renderer_version",
    "renderer_build",
    "renderer_artifact",
    "renderer_license",
    "source_profile",
    "source_color",
    "output_profile",
    "semantic_commands",
    "render_primitives",
    "embedded_audio",
    "resource_limits",
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_ORIGINS: Final = ("context_video", "runtime_image", "generated")
_SOURCE_DISPOSITIONS: Final = ("enabled", "unregistered")
_MAX_SOURCE_BYTES: Final = 64 * 1024 * 1024
_MAX_SOURCE_SET_BYTES: Final = 256 * 1024 * 1024
_MAX_SOURCE_DURATION_MILLISECONDS: Final = 30_000
_MAX_SOURCE_EDGE: Final = 8_192
_MAX_SOURCE_PIXELS: Final = 4_194_304
_MAX_TITLE_BINDINGS: Final = 128
_FRAMES_PER_CHUNK: Final = 64
_MAX_OPERATION_CHUNKS: Final = 256
_MAX_PLAN_FRAMES: Final = _FRAMES_PER_CHUNK * _MAX_OPERATION_CHUNKS
_MAX_RESOLVED_OPERATIONS: Final = 1_000_000
_MAX_PLAN_BYTES: Final = 16 * 1024 * 1024


class RenderPlannerError(ContractValidationError):
    """One typed, content-free render-planning refusal."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str) -> RenderPlannerError:
    return RenderPlannerError(code, message)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_contract", f"{field} must be a bounded identifier")
    return value


def _fingerprint(value: object, field: str) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_contract", f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _reject("resource_limit", f"{field} is outside its closed bound")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivateSourceFact:
    """A content-free adapter claim for one registry source."""

    asset_id: str
    source_id: str
    origin: str
    disposition: str
    generation: int
    source_fingerprint: str | None
    source_facts_fingerprint: str | None
    source_profile_fingerprint: str | None
    color_facts_fingerprint: str | None
    currentness_token: str | None
    lease_fingerprint: str | None
    width: int | None
    height: int | None
    size_bytes: int
    duration_milliseconds: int | None

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "origin": self.origin,
            "disposition": self.disposition,
            "generation": self.generation,
            "source_fingerprint": self.source_fingerprint,
            "source_facts_fingerprint": self.source_facts_fingerprint,
            "source_profile_fingerprint": self.source_profile_fingerprint,
            "color_facts_fingerprint": self.color_facts_fingerprint,
            "currentness_token": self.currentness_token,
            "lease_fingerprint": self.lease_fingerprint,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "duration_milliseconds": self.duration_milliseconds,
        }


def source_facts_fingerprint(
    *,
    asset_id: str,
    source_id: str,
    origin: str,
    width: int,
    height: int,
    size_bytes: int,
    duration_milliseconds: int | None,
    source_profile_fingerprint: str,
    color_facts_fingerprint: str,
) -> str:
    """Fingerprint the exact dimension/profile/color facts measured by an adapter."""

    return canonical_fingerprint(
        {
            "asset_id": asset_id,
            "source_id": source_id,
            "origin": origin,
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "duration_milliseconds": duration_milliseconds,
            "source_profile_fingerprint": source_profile_fingerprint,
            "color_facts_fingerprint": color_facts_fingerprint,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivateSourceFactsManifest:
    schema_version: str
    workspace_handle: str
    workspace_revision: int
    timeline_revision: int
    public_fingerprint: str
    generation: int
    facts: tuple[PrivateSourceFact, ...]
    manifest_fingerprint: str

    def fingerprint_material(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_handle": self.workspace_handle,
            "workspace_revision": self.workspace_revision,
            "timeline_revision": self.timeline_revision,
            "public_fingerprint": self.public_fingerprint,
            "generation": self.generation,
            "facts": [fact.to_wire() for fact in self.facts],
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.fingerprint_material(), "manifest_fingerprint": self.manifest_fingerprint}


def private_source_manifest_fingerprint(manifest: PrivateSourceFactsManifest) -> str:
    if type(manifest) is not PrivateSourceFactsManifest:
        raise _reject("invalid_contract", "source manifest must be typed")
    return canonical_fingerprint(manifest.fingerprint_material())


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceCurrentnessToken:
    asset_id: str
    source_fingerprint: str
    source_facts_fingerprint: str
    currentness_token: str
    lease_fingerprint: str

    def to_wire(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "source_fingerprint": self.source_fingerprint,
            "source_facts_fingerprint": self.source_facts_fingerprint,
            "currentness_token": self.currentness_token,
            "lease_fingerprint": self.lease_fingerprint,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceCurrentnessConfirmation:
    schema_version: str
    manifest_fingerprint: str
    workspace_handle: str
    workspace_revision: int
    timeline_revision: int
    public_fingerprint: str
    generation: int
    tokens: tuple[SourceCurrentnessToken, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_fingerprint": self.manifest_fingerprint,
            "workspace_handle": self.workspace_handle,
            "workspace_revision": self.workspace_revision,
            "timeline_revision": self.timeline_revision,
            "public_fingerprint": self.public_fingerprint,
            "generation": self.generation,
            "tokens": [token.to_wire() for token in self.tokens],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FontTitleBindingFact:
    """Proof that the packaged reader resolved one title's exact text and style."""

    clip_id: str
    font_asset_id: str
    artifact_id: str
    weight: int
    style: str
    text_fingerprint: str
    file_fingerprint: str
    cmap_fingerprint: str
    build_version: str

    def to_wire(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "font_asset_id": self.font_asset_id,
            "artifact_id": self.artifact_id,
            "weight": self.weight,
            "style": self.style,
            "text_fingerprint": self.text_fingerprint,
            "file_fingerprint": self.file_fingerprint,
            "cmap_fingerprint": self.cmap_fingerprint,
            "build_version": self.build_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PackagedFontFacts:
    """Adapter-neutral facts measured from the repository-packaged font manifest."""

    schema_version: str
    manifest_schema_version: str
    profile_id: str
    fallback_order: tuple[str, ...]
    license_spdx_id: str
    manifest_fingerprint: str
    package_fingerprint: str
    license_file_fingerprint: str
    title_bindings: tuple[FontTitleBindingFact, ...]
    facts_fingerprint: str

    def fingerprint_material(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_schema_version": self.manifest_schema_version,
            "profile_id": self.profile_id,
            "fallback_order": list(self.fallback_order),
            "license_spdx_id": self.license_spdx_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "package_fingerprint": self.package_fingerprint,
            "license_file_fingerprint": self.license_file_fingerprint,
            "title_bindings": [binding.to_wire() for binding in self.title_bindings],
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.fingerprint_material(), "facts_fingerprint": self.facts_fingerprint}


def packaged_font_facts_fingerprint(facts: PackagedFontFacts) -> str:
    if type(facts) is not PackagedFontFacts:
        raise _reject("invalid_contract", "font facts must be typed")
    return canonical_fingerprint(facts.fingerprint_material())


@dataclass(frozen=True, slots=True, kw_only=True)
class RendererOutputDeclaration:
    profile_id: str
    frame_rate_num: int
    frame_rate_den: int
    time_base_num: int
    time_base_den: int
    max_width: int
    max_height: int
    max_pixels: int
    dimensions_multiple: int
    container: str
    video_codec: str
    pixel_format: str
    pixel_aspect_num: int
    pixel_aspect_den: int
    color_policy: str
    audio_policy: str
    audio_codec: str
    sample_rate: int
    channels: int
    preview_max_av_drift_samples: int
    final_impulse_tolerance_samples: int

    def to_wire(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "frame_rate": {"num": self.frame_rate_num, "den": self.frame_rate_den},
            "time_base": {"num": self.time_base_num, "den": self.time_base_den},
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_pixels": self.max_pixels,
            "dimensions_multiple": self.dimensions_multiple,
            "container": self.container,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "pixel_aspect": {"num": self.pixel_aspect_num, "den": self.pixel_aspect_den},
            "color_policy": self.color_policy,
            "audio_policy": self.audio_policy,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "preview_max_av_drift_samples": self.preview_max_av_drift_samples,
            "final_impulse_tolerance_samples": self.final_impulse_tolerance_samples,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IndependentAudioExtensionSeam:
    schema_version: str
    track_profile: str
    command_namespace: str
    command_members: tuple[str, ...]
    embedded_renderer_variant: str
    independent_audio_renderer_variant: str

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "track_profile": self.track_profile,
            "command_namespace": self.command_namespace,
            "command_members": list(self.command_members),
            "embedded_renderer_variant": self.embedded_renderer_variant,
            "independent_audio_renderer_variant": self.independent_audio_renderer_variant,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderPlanLimits:
    max_sources: int
    max_source_bytes: int
    max_source_set_bytes: int
    max_source_duration_milliseconds: int
    max_source_edge: int
    max_source_pixels: int
    max_title_bindings: int
    frames_per_chunk: int
    max_operation_chunks: int
    max_plan_frames: int
    max_resolved_operations: int
    max_plan_bytes: int

    def to_wire(self) -> dict[str, int]:
        return {
            "max_sources": self.max_sources,
            "max_source_bytes": self.max_source_bytes,
            "max_source_set_bytes": self.max_source_set_bytes,
            "max_source_duration_milliseconds": self.max_source_duration_milliseconds,
            "max_source_edge": self.max_source_edge,
            "max_source_pixels": self.max_source_pixels,
            "max_title_bindings": self.max_title_bindings,
            "frames_per_chunk": self.frames_per_chunk,
            "max_operation_chunks": self.max_operation_chunks,
            "max_plan_frames": self.max_plan_frames,
            "max_resolved_operations": self.max_resolved_operations,
            "max_plan_bytes": self.max_plan_bytes,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RendererQualificationRow:
    row_id: str
    disposition: str
    evidence_fingerprint: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "disposition": self.disposition,
            "evidence_fingerprint": self.evidence_fingerprint,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RendererCapabilityProfile:
    schema_version: str
    profile_id: str
    required_engine_profile_id: str
    input_manifest_schema_version: str
    qualification: str
    execution_capability: str
    renderer_id: str | None
    renderer_version: str | None
    renderer_build_fingerprint: str | None
    renderer_artifact_fingerprint: str | None
    renderer_license_spdx_id: str | None
    source_origins: tuple[str, ...]
    source_profile_authority: str
    semantic_command_ids: tuple[str, ...]
    render_primitive_ids: tuple[str, ...]
    qualification_rows: tuple[RendererQualificationRow, ...]
    output: RendererOutputDeclaration
    audio_extension: IndependentAudioExtensionSeam
    limits: RenderPlanLimits
    profile_fingerprint: str

    def fingerprint_material(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "required_engine_profile_id": self.required_engine_profile_id,
            "input_manifest_schema_version": self.input_manifest_schema_version,
            "qualification": self.qualification,
            "execution_capability": self.execution_capability,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "renderer_build_fingerprint": self.renderer_build_fingerprint,
            "renderer_artifact_fingerprint": self.renderer_artifact_fingerprint,
            "renderer_license_spdx_id": self.renderer_license_spdx_id,
            "source_origins": list(self.source_origins),
            "source_profile_authority": self.source_profile_authority,
            "semantic_command_ids": list(self.semantic_command_ids),
            "render_primitive_ids": list(self.render_primitive_ids),
            "qualification_rows": [row.to_wire() for row in self.qualification_rows],
            "output": self.output.to_wire(),
            "audio_extension": self.audio_extension.to_wire(),
            "limits": self.limits.to_wire(),
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.fingerprint_material(), "profile_fingerprint": self.profile_fingerprint}


def required_unqualified_renderer_profile() -> RendererCapabilityProfile:
    """Return the exact M25-17 requirement without claiming an executable exists."""

    provisional = RendererCapabilityProfile(
        schema_version=RENDERER_PROFILE_SCHEMA,
        profile_id=RENDERER_PROFILE_ID,
        required_engine_profile_id=ENGINE_PROFILE_ID,
        input_manifest_schema_version=PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA,
        qualification="unqualified",
        execution_capability="unavailable",
        renderer_id=None,
        renderer_version=None,
        renderer_build_fingerprint=None,
        renderer_artifact_fingerprint=None,
        renderer_license_spdx_id=None,
        source_origins=("context_video", "runtime_image"),
        source_profile_authority="source_facts_fingerprint_v1",
        semantic_command_ids=NLE_OPERATION_IDS,
        render_primitive_ids=RENDER_PRIMITIVE_IDS,
        qualification_rows=tuple(
            RendererQualificationRow(
                row_id=row_id,
                disposition="unqualified",
                evidence_fingerprint=None,
            )
            for row_id in RENDERER_QUALIFICATION_ROW_IDS
        ),
        output=RendererOutputDeclaration(
            profile_id=OUTPUT_PROFILE_ID,
            frame_rate_num=24,
            frame_rate_den=1,
            time_base_num=1,
            time_base_den=24,
            max_width=1_920,
            max_height=1_080,
            max_pixels=2_073_600,
            dimensions_multiple=2,
            container="mp4",
            video_codec="h264",
            pixel_format="yuv420p",
            pixel_aspect_num=1,
            pixel_aspect_den=1,
            color_policy="bt709_sdr_limited_v1",
            audio_policy="primary_embedded_follow_video_v1",
            audio_codec="aac",
            sample_rate=48_000,
            channels=1,
            preview_max_av_drift_samples=2_000,
            final_impulse_tolerance_samples=2_048,
        ),
        audio_extension=IndependentAudioExtensionSeam(
            schema_version=AUDIO_EXTENSION_SCHEMA,
            track_profile="none_v1",
            command_namespace=INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
            command_members=(),
            embedded_renderer_variant="EmbeddedAudioSpanV1",
            independent_audio_renderer_variant="none_v1",
        ),
        limits=RenderPlanLimits(
            max_sources=MAX_ASSETS,
            max_source_bytes=_MAX_SOURCE_BYTES,
            max_source_set_bytes=_MAX_SOURCE_SET_BYTES,
            max_source_duration_milliseconds=_MAX_SOURCE_DURATION_MILLISECONDS,
            max_source_edge=_MAX_SOURCE_EDGE,
            max_source_pixels=_MAX_SOURCE_PIXELS,
            max_title_bindings=_MAX_TITLE_BINDINGS,
            frames_per_chunk=_FRAMES_PER_CHUNK,
            max_operation_chunks=_MAX_OPERATION_CHUNKS,
            max_plan_frames=_MAX_PLAN_FRAMES,
            max_resolved_operations=_MAX_RESOLVED_OPERATIONS,
            max_plan_bytes=_MAX_PLAN_BYTES,
        ),
        profile_fingerprint="sha256:" + "0" * 64,
    )
    return replace(
        provisional,
        profile_fingerprint=canonical_fingerprint(provisional.fingerprint_material()),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedFrameOperations:
    frame: int
    layers: tuple[ResolvedLayer, ...]
    audio_span: EmbeddedAudioSpan | None
    audio_operation_id: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "frame": self.frame,
            "layers": [layer.to_wire() for layer in self.layers],
            "audio_span": None if self.audio_span is None else self.audio_span.to_wire(),
            "audio_operation_id": self.audio_operation_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedOperationChunk:
    start_frame: int
    frames: tuple[ResolvedFrameOperations, ...]
    chunk_fingerprint: str

    def fingerprint_material(self) -> dict[str, object]:
        return {
            "start_frame": self.start_frame,
            "frames": [frame.to_wire() for frame in self.frames],
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.fingerprint_material(), "chunk_fingerprint": self.chunk_fingerprint}


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanClipAudio:
    """One clip's audio adjustments, as the final render applies them (`core/clip_audio.py`)."""

    clip_id: str
    start_frame: int
    duration_frames: int
    audio: ClipAudio

    def to_wire(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "start_frame": self.start_frame,
            "duration_frames": self.duration_frames,
            "audio": self.audio.to_wire(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderPlanV1:
    schema_version: str
    snapshot_revision: int
    snapshot_fingerprint: str
    source_bindings: tuple[PrivateSourceFact, ...]
    source_manifest_fingerprint: str
    font_manifest_fingerprint: str
    font_package_fingerprint: str
    font_facts_fingerprint: str
    renderer_capability_profile_fingerprint: str
    resolved_operations: tuple[ResolvedOperationChunk, ...]
    output_profile: OutputProfile
    idempotency_fingerprint: str
    source_currentness_claim: SourceCurrentnessConfirmation
    limits: RenderPlanLimits
    emits_audio_stream: bool
    unavailable_disposition: str
    # Every clip with a non-identity audio member, in clip order.
    clip_audio: tuple[PlanClipAudio, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_revision": self.snapshot_revision,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "source_bindings": [binding.to_wire() for binding in self.source_bindings],
            "source_manifest_fingerprint": self.source_manifest_fingerprint,
            "font_manifest_fingerprint": self.font_manifest_fingerprint,
            "font_package_fingerprint": self.font_package_fingerprint,
            "font_facts_fingerprint": self.font_facts_fingerprint,
            "renderer_capability_profile_fingerprint": (
                self.renderer_capability_profile_fingerprint
            ),
            "resolved_operations": [chunk.to_wire() for chunk in self.resolved_operations],
            "output_profile": self.output_profile.to_wire(),
            "idempotency_fingerprint": self.idempotency_fingerprint,
            "source_currentness_claim": self.source_currentness_claim.to_wire(),
            "limits": self.limits.to_wire(),
            "emits_audio_stream": self.emits_audio_stream,
            "unavailable_disposition": self.unavailable_disposition,
            # GUARD: written only when a clip carries an adjustment, so that a plan of a
            # composition without one keeps its bytes and its fingerprint.
            **(
                {"clip_audio": [row.to_wire() for row in self.clip_audio]}
                if self.clip_audio
                else {}
            ),
        }


def _validate_source_manifest(
    snapshot: PublicCompositionSnapshot,
    manifest: PrivateSourceFactsManifest,
) -> dict[str, PrivateSourceFact]:
    if type(manifest) is not PrivateSourceFactsManifest:
        raise _reject("invalid_contract", "source manifest must be typed")
    if manifest.schema_version != PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA:
        raise _reject("unsupported_profile", "source manifest schema is unsupported")
    _identifier(manifest.workspace_handle, "source manifest workspace")
    _integer(
        manifest.workspace_revision, "source manifest workspace revision", 1, MAX_EXTENT_FRAMES
    )
    _integer(manifest.timeline_revision, "source manifest timeline revision", 1, MAX_EXTENT_FRAMES)
    _integer(manifest.generation, "source manifest generation", 0, MAX_EXTENT_FRAMES)
    _fingerprint(manifest.public_fingerprint, "source manifest public fingerprint")
    _fingerprint(manifest.manifest_fingerprint, "source manifest fingerprint")
    if (
        manifest.workspace_handle != snapshot.workspace_handle
        or manifest.workspace_revision != snapshot.workspace_revision
        or manifest.timeline_revision != snapshot.timeline_revision
        or manifest.public_fingerprint != snapshot.public_fingerprint
    ):
        raise _reject("stale_snapshot", "source manifest does not bind the accepted snapshot")
    if type(manifest.facts) is not tuple or len(manifest.facts) > MAX_ASSETS:
        raise _reject("resource_limit", "source manifest count is outside its closed bound")
    if any(type(fact) is not PrivateSourceFact for fact in manifest.facts):
        raise _reject("invalid_contract", "source manifest row must be typed")
    if (not manifest.facts and manifest.generation != 0) or (
        manifest.facts and manifest.generation == 0
    ):
        raise _reject("source_replaced", "source manifest generation does not match its facts")
    if tuple(fact.asset_id for fact in manifest.facts) != tuple(
        sorted(fact.asset_id for fact in manifest.facts)
    ):
        raise _reject("invalid_contract", "source facts must use canonical asset order")
    if manifest.manifest_fingerprint != private_source_manifest_fingerprint(manifest):
        raise _reject("source_facts_mismatch", "source manifest fingerprint changed")

    expected_assets = {asset.asset_id: asset for asset in snapshot.assets if asset.kind != "font"}
    if set(expected_assets) != {fact.asset_id for fact in manifest.facts}:
        raise _reject("source_unavailable", "source manifest does not cover exact media assets")

    facts_by_id: dict[str, PrivateSourceFact] = {}
    aggregate_bytes = 0
    for fact in manifest.facts:
        _identifier(fact.asset_id, "source asset ID")
        _identifier(fact.source_id, "source ID")
        _integer(fact.generation, "source generation", 1, MAX_EXTENT_FRAMES)
        if fact.asset_id != fact.source_id or fact.asset_id in facts_by_id:
            raise _reject("source_unavailable", "source identity does not match the registry asset")
        if fact.origin not in _SOURCE_ORIGINS or fact.disposition not in _SOURCE_DISPOSITIONS:
            raise _reject("invalid_contract", "source origin or disposition is unsupported")
        if fact.generation != manifest.generation:
            raise _reject("source_replaced", "source generation changed")
        asset = expected_assets[fact.asset_id]
        # IMPORTANT: generated authority is a VIDEO-only import origin. Keep IMAGE on the
        # runtime-image path and the authority-free unregistered branch below, or forged source
        # kinds can cross the render boundary under a valid-looking generated fingerprint.
        enabled_origins = (
            ("context_video", "generated") if asset.kind == "video" else ("runtime_image",)
        )
        if fact.disposition == "enabled" and fact.origin not in enabled_origins:
            raise _reject(
                "source_origin_mismatch", "enabled source origin does not match asset kind"
            )
        if fact.disposition == "unregistered":
            if (
                fact.origin != "generated"
                or any(
                    value is not None
                    for value in (
                        fact.source_fingerprint,
                        fact.source_facts_fingerprint,
                        fact.source_profile_fingerprint,
                        fact.color_facts_fingerprint,
                        fact.currentness_token,
                        fact.lease_fingerprint,
                        fact.width,
                        fact.height,
                        fact.duration_milliseconds,
                    )
                )
                or fact.size_bytes != 0
            ):
                raise _reject("invalid_contract", "unregistered generated source carries authority")
            facts_by_id[fact.asset_id] = fact
            continue
        _fingerprint(fact.source_fingerprint, "source fingerprint")
        source_facts_fp = _fingerprint(fact.source_facts_fingerprint, "source facts fingerprint")
        source_profile_fp = _fingerprint(
            fact.source_profile_fingerprint, "source profile fingerprint"
        )
        color_facts_fp = _fingerprint(
            fact.color_facts_fingerprint, "source color facts fingerprint"
        )
        _fingerprint(fact.lease_fingerprint, "source lease fingerprint")
        _identifier(fact.currentness_token, "source currentness token")
        width = _integer(fact.width, "source width", 1, _MAX_SOURCE_EDGE)
        height = _integer(fact.height, "source height", 1, _MAX_SOURCE_EDGE)
        if width * height > _MAX_SOURCE_PIXELS:
            raise _reject("resource_limit", "source dimensions exceed the pixel bound")
        size_bytes = _integer(fact.size_bytes, "source bytes", 1, _MAX_SOURCE_BYTES)
        aggregate_bytes += size_bytes
        if aggregate_bytes > _MAX_SOURCE_SET_BYTES:
            raise _reject("resource_limit", "source set exceeds its byte bound")
        if asset.kind == "video":
            duration = _integer(
                fact.duration_milliseconds,
                "source duration",
                1,
                _MAX_SOURCE_DURATION_MILLISECONDS,
            )
        elif fact.duration_milliseconds is not None:
            raise _reject("invalid_contract", "runtime image source carries duration")
        else:
            duration = None
        expected_facts = source_facts_fingerprint(
            asset_id=fact.asset_id,
            source_id=fact.source_id,
            origin=fact.origin,
            width=width,
            height=height,
            size_bytes=size_bytes,
            duration_milliseconds=duration,
            source_profile_fingerprint=source_profile_fp,
            color_facts_fingerprint=color_facts_fp,
        )
        if expected_facts != source_facts_fp:
            # CRITICAL: dimensions and source/color profiles are currentness facts. Accepting a
            # content hash alone would let replacement metadata drift into render geometry.
            raise _reject("source_facts_mismatch", "measured source facts changed")
        facts_by_id[fact.asset_id] = fact
    return facts_by_id


def _validate_currentness(
    manifest: PrivateSourceFactsManifest,
    confirmation: SourceCurrentnessConfirmation,
) -> None:
    if type(confirmation) is not SourceCurrentnessConfirmation:
        raise _reject("invalid_contract", "source currentness confirmation must be typed")
    if confirmation.schema_version != SOURCE_CURRENTNESS_SCHEMA:
        raise _reject("unsupported_profile", "source currentness schema is unsupported")
    header_matches = (
        confirmation.manifest_fingerprint == manifest.manifest_fingerprint
        and confirmation.workspace_handle == manifest.workspace_handle
        and confirmation.workspace_revision == manifest.workspace_revision
        and confirmation.timeline_revision == manifest.timeline_revision
        and confirmation.public_fingerprint == manifest.public_fingerprint
    )
    if not header_matches:
        raise _reject("stale_snapshot", "source currentness does not bind the plan snapshot")
    if confirmation.generation != manifest.generation:
        raise _reject("source_replaced", "source currentness generation changed")
    enabled = tuple(fact for fact in manifest.facts if fact.disposition == "enabled")
    if type(confirmation.tokens) is not tuple or len(confirmation.tokens) != len(enabled):
        raise _reject("source_replaced", "source currentness token set changed")
    expected_tokens: list[SourceCurrentnessToken] = []
    for fact in enabled:
        source_fingerprint = _fingerprint(fact.source_fingerprint, "source fingerprint")
        source_facts_fp = _fingerprint(fact.source_facts_fingerprint, "source facts fingerprint")
        currentness_token = _identifier(fact.currentness_token, "source currentness token")
        lease_fingerprint = _fingerprint(fact.lease_fingerprint, "source lease fingerprint")
        expected_tokens.append(
            SourceCurrentnessToken(
                asset_id=fact.asset_id,
                source_fingerprint=source_fingerprint,
                source_facts_fingerprint=source_facts_fp,
                currentness_token=currentness_token,
                lease_fingerprint=lease_fingerprint,
            )
        )
    if confirmation.tokens != tuple(expected_tokens):
        raise _reject("source_replaced", "source currentness facts changed")


def _validate_font_facts(
    snapshot: PublicCompositionSnapshot,
    facts: PackagedFontFacts,
) -> None:
    if type(facts) is not PackagedFontFacts:
        raise _reject("invalid_contract", "font facts must be typed")
    if (
        facts.schema_version != FONT_FACTS_SCHEMA
        or facts.manifest_schema_version != PACKAGED_FONT_MANIFEST_SCHEMA
        or facts.profile_id != PACKAGED_FONT_PROFILE_ID
    ):
        raise _reject("font_manifest_invalid", "packaged font profile is unsupported")
    if facts.license_spdx_id != "OFL-1.1":
        raise _reject("font_manifest_invalid", "packaged font license is unsupported")
    for value, field in (
        (facts.manifest_fingerprint, "font manifest fingerprint"),
        (facts.package_fingerprint, "font package fingerprint"),
        (facts.license_file_fingerprint, "font license fingerprint"),
        (facts.facts_fingerprint, "font facts fingerprint"),
    ):
        _fingerprint(value, field)
    if (
        type(facts.fallback_order) is not tuple
        or not facts.fallback_order
        or len(facts.fallback_order) > 16
        or len(set(facts.fallback_order)) != len(facts.fallback_order)
    ):
        raise _reject("font_manifest_invalid", "packaged font fallback order is invalid")
    for font_asset_id in facts.fallback_order:
        _identifier(font_asset_id, "font fallback asset ID")
    if type(facts.title_bindings) is not tuple or len(facts.title_bindings) > _MAX_TITLE_BINDINGS:
        raise _reject("resource_limit", "font title binding count exceeds its bound")
    if any(type(binding) is not FontTitleBindingFact for binding in facts.title_bindings):
        raise _reject("font_manifest_invalid", "font title binding must be typed")
    if tuple(binding.clip_id for binding in facts.title_bindings) != tuple(
        sorted(binding.clip_id for binding in facts.title_bindings)
    ):
        raise _reject("font_manifest_invalid", "font title bindings must use canonical clip order")
    if facts.facts_fingerprint != packaged_font_facts_fingerprint(facts):
        raise _reject("font_manifest_invalid", "packaged font facts changed")

    tracks = {track.track_id: track for track in snapshot.tracks}
    expected_titles = tuple(
        clip
        for clip in snapshot.clips
        if clip.enabled
        and tracks[clip.track_id].enabled
        and tracks[clip.track_id].kind == "text_overlay"
        and clip.start_frame < snapshot.output.duration_frames
    )
    bindings = {binding.clip_id: binding for binding in facts.title_bindings}
    if len(bindings) != len(facts.title_bindings):
        raise _reject("font_manifest_invalid", "font title binding IDs are not unique")
    if set(bindings) != {clip.clip_id for clip in expected_titles}:
        raise _reject("font_glyph_unsupported", "an active title lacks packaged glyph authority")
    for clip in expected_titles:
        if clip.text is None:
            raise _reject("invalid_contract", "text track clip lacks text")
        binding = bindings[clip.clip_id]
        _identifier(binding.clip_id, "font title clip ID")
        _identifier(binding.font_asset_id, "font asset ID")
        _identifier(binding.artifact_id, "font artifact ID")
        _fingerprint(binding.text_fingerprint, "font title text fingerprint")
        _fingerprint(binding.file_fingerprint, "font file fingerprint")
        _fingerprint(binding.cmap_fingerprint, "font cmap fingerprint")
        if (
            not isinstance(binding.build_version, str)
            or not 1 <= len(binding.build_version) <= 128
            or any(ord(character) < 32 for character in binding.build_version)
        ):
            raise _reject("font_manifest_invalid", "font build version is invalid")
        if binding.font_asset_id not in facts.fallback_order:
            raise _reject("font_asset_unsupported", "title font is outside packaged fallback order")
        if binding.weight != clip.text.weight or binding.style != clip.text.style:
            raise _reject("font_style_unsupported", "title style lacks packaged font authority")
        if binding.font_asset_id != clip.text.font_asset_id or binding.text_fingerprint != (
            canonical_fingerprint({"content": clip.text.content})
        ):
            raise _reject("font_glyph_unsupported", "title text lacks packaged glyph authority")


def _validate_renderer_profile(profile: RendererCapabilityProfile) -> RendererCapabilityProfile:
    if type(profile) is not RendererCapabilityProfile:
        raise _reject("invalid_contract", "renderer profile must be typed")
    if profile.qualification != "unqualified" or profile.execution_capability != "unavailable":
        raise _reject(
            "renderer_profile_unqualified",
            "M25-17 cannot advertise renderer execution capability",
        )
    if any(
        value is not None
        for value in (
            profile.renderer_id,
            profile.renderer_version,
            profile.renderer_build_fingerprint,
            profile.renderer_artifact_fingerprint,
            profile.renderer_license_spdx_id,
        )
    ):
        raise _reject(
            "renderer_profile_unqualified",
            "unqualified renderer profile cannot carry invented implementation facts",
        )
    expected = required_unqualified_renderer_profile()
    if profile != expected:
        raise _reject("unsupported_profile", "renderer capability declaration drifted")
    return expected


def _validate_output(profile: RendererOutputDeclaration, output: OutputProfile) -> None:
    if (
        output.profile_id != profile.profile_id
        or output.frame_rate.num != profile.frame_rate_num
        or output.frame_rate.den != profile.frame_rate_den
        or output.time_base.num != profile.time_base_num
        or output.time_base.den != profile.time_base_den
        or output.width > profile.max_width
        or output.height > profile.max_height
        or output.width * output.height > profile.max_pixels
        or output.width % profile.dimensions_multiple
        or output.height % profile.dimensions_multiple
        or output.container != profile.container
        or output.video_codec != profile.video_codec
        or output.pixel_format != profile.pixel_format
        or output.pixel_aspect.num != profile.pixel_aspect_num
        or output.pixel_aspect.den != profile.pixel_aspect_den
        or output.color_policy != profile.color_policy
        or output.audio_policy != profile.audio_policy
        or output.audio_codec != profile.audio_codec
        or output.sample_rate != profile.sample_rate
        or output.channels != profile.channels
        or output.preview_max_av_drift_samples != profile.preview_max_av_drift_samples
        or output.final_impulse_tolerance_samples != profile.final_impulse_tolerance_samples
    ):
        raise _reject("unsupported_output_profile", "composition output profile drifted")


def _resolve_operation_chunks(
    snapshot: PublicCompositionSnapshot,
    limits: RenderPlanLimits,
) -> tuple[tuple[ResolvedOperationChunk, ...], bool, int]:
    if snapshot.output.duration_frames > limits.max_plan_frames:
        raise _reject("resource_limit", "render duration exceeds the planner frame bound")
    tracks = {track.track_id: track for track in snapshot.tracks}
    assets = {asset.asset_id: asset for asset in snapshot.assets}
    for clip in snapshot.clips:
        if (
            clip.enabled
            and tracks[clip.track_id].enabled
            and clip.asset_id is not None
            and assets[clip.asset_id].kind == "video"
        ):
            try:
                resolve_source_interval_coverage(
                    assets[clip.asset_id],
                    clip.source_start_frame,
                    clip.duration_frames,
                    snapshot.output.frame_rate,
                )
            except CompositionContractError as exc:
                raise _reject(exc.code, "render source interval is unavailable") from exc

    chunks: list[ResolvedOperationChunk] = []
    pending: list[ResolvedFrameOperations] = []
    operation_count = 0
    total_bytes = 0
    emits_audio = False
    for frame in range(snapshot.output.duration_frames):
        try:
            scene = resolve_composition(snapshot, frame)
        except CompositionContractError as exc:
            raise _reject(exc.code, "composition frame cannot be resolved") from exc
        if scene.blockers:
            raise _reject(scene.blockers[0].code, "composition frame contains a render blocker")
        for layer in scene.layers:
            if not set(layer.operation_ids) <= set(RENDER_PRIMITIVE_IDS):
                raise _reject(
                    "operation_not_in_profile", "resolved layer contains an unknown operation"
                )
            indexes = tuple(RENDER_PRIMITIVE_IDS.index(item) for item in layer.operation_ids)
            if indexes != tuple(sorted(set(indexes))):
                raise _reject(
                    "operation_not_in_profile", "resolved operations are not in closed order"
                )
            operation_count += len(layer.operation_ids)
        audio_operation_id = None
        if scene.audio_span is not None:
            emits_audio = True
            audio_operation_id = "PrimaryEmbeddedFollowVideoV1"
            operation_count += 1
        if operation_count > limits.max_resolved_operations:
            raise _reject("resource_limit", "resolved operation count exceeds its bound")
        pending.append(
            ResolvedFrameOperations(
                frame=frame,
                layers=scene.layers,
                audio_span=scene.audio_span,
                audio_operation_id=audio_operation_id,
            )
        )
        if len(pending) == limits.frames_per_chunk or frame + 1 == snapshot.output.duration_frames:
            provisional = ResolvedOperationChunk(
                start_frame=pending[0].frame,
                frames=tuple(pending),
                chunk_fingerprint="sha256:" + "0" * 64,
            )
            try:
                material_bytes = canonical_bytes(provisional.fingerprint_material())
                fingerprint = canonical_fingerprint(provisional.fingerprint_material())
            except CanonicalizationError as exc:
                raise _reject(
                    "resource_limit", "resolved operation chunk exceeds its bound"
                ) from exc
            total_bytes += len(material_bytes)
            if total_bytes > limits.max_plan_bytes:
                raise _reject("resource_limit", "resolved render plan exceeds its byte bound")
            chunks.append(replace(provisional, chunk_fingerprint=fingerprint))
            pending = []
    if len(chunks) > limits.max_operation_chunks:
        raise _reject("resource_limit", "render plan chunk count exceeds its bound")
    return tuple(chunks), emits_audio, total_bytes


def plan_render(
    *,
    snapshot: PublicCompositionSnapshot,
    source_manifest: PrivateSourceFactsManifest,
    currentness: SourceCurrentnessConfirmation,
    font_facts: PackagedFontFacts,
    renderer_profile: RendererCapabilityProfile | None = None,
) -> RenderPlanV1:
    """Build one deterministic plan without executing or qualifying a renderer."""

    if type(snapshot) is not PublicCompositionSnapshot:
        raise _reject("invalid_contract", "render planner requires a typed public snapshot")
    audio = snapshot.audio_extension
    if (
        audio.schema,
        audio.track_profile,
        audio.command_namespace,
        audio.command_members,
        audio.preview_edit_capability,
        audio.final_render_edit_capability,
        audio.embedded_renderer_variant,
        audio.independent_audio_renderer_variant,
        audio.reason,
    ) != (
        AUDIO_EXTENSION_SCHEMA,
        "none_v1",
        INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
        (),
        "unsupported",
        "unsupported",
        "EmbeddedAudioSpanV1",
        "none_v1",
        "audio_editing_deferred",
    ):
        raise _reject("audio_editing_deferred", "independent audio extension is closed")
    try:
        # Re-decode the wire form so dataclass replacement cannot bypass the accepted closed codec.
        accepted = decode_public_snapshot(snapshot.to_wire())
    except CompositionContractError as exc:
        raise _reject(exc.code, "public composition snapshot is not accepted") from exc
    # CRITICAL: a historical overlay-policy tag is not measured audio absence. Keep old snapshots
    # readable, but require genuine source reinitialization instead of inventing facts for a job.
    if any(asset.embedded_audio == "excluded_overlay_policy" for asset in accepted.assets):
        raise _reject("source_unavailable", "legacy source policy requires measured source facts")
    profile = _validate_renderer_profile(
        required_unqualified_renderer_profile() if renderer_profile is None else renderer_profile
    )
    _validate_output(profile.output, accepted.output)
    source_by_id = _validate_source_manifest(accepted, source_manifest)
    _validate_currentness(source_manifest, currentness)
    _validate_font_facts(accepted, font_facts)

    for asset in accepted.assets:
        if asset.kind != "video" or source_by_id[asset.asset_id].disposition != "enabled":
            continue
        if (
            asset.source_frame_count is None
            or len(asset.landmarks) != asset.source_frame_count
            or (
                tuple(landmark.frame_index for landmark in asset.landmarks)
                != tuple(range(asset.source_frame_count))
            )
        ):
            # CRITICAL: sparse CFR extrapolation is not source authority. Every admitted video
            # frame needs its measured PTS/duration row or VFR selection can silently seek wrong.
            raise _reject(
                "source_range_unavailable",
                "video source lacks a complete bounded timing table",
            )

    tracks = {track.track_id: track for track in accepted.tracks}
    for clip in accepted.clips:
        if clip.enabled and tracks[clip.track_id].enabled and clip.asset_id is not None:
            asset = next(asset for asset in accepted.assets if asset.asset_id == clip.asset_id)
            if asset.kind != "font" and source_by_id[asset.asset_id].disposition != "enabled":
                raise _reject("source_unavailable", "an active clip source is unregistered")

    chunks, emits_audio, _resolved_bytes = _resolve_operation_chunks(accepted, profile.limits)
    clip_audio = tuple(
        PlanClipAudio(
            clip_id=clip.clip_id,
            start_frame=clip.start_frame,
            duration_frames=clip.duration_frames,
            audio=clip.audio,
        )
        for clip in accepted.clips
        if clip.audio != IDENTITY_CLIP_AUDIO
    )
    idempotency_fingerprint = canonical_fingerprint(
        {
            "schema_version": RENDER_PLAN_SCHEMA,
            "snapshot_revision": accepted.timeline_revision,
            "snapshot_fingerprint": accepted.public_fingerprint,
            "source_manifest_fingerprint": source_manifest.manifest_fingerprint,
            "font_manifest_fingerprint": font_facts.manifest_fingerprint,
            "font_package_fingerprint": font_facts.package_fingerprint,
            "font_facts_fingerprint": font_facts.facts_fingerprint,
            "renderer_capability_profile_fingerprint": profile.profile_fingerprint,
            "resolved_operation_chunk_fingerprints": [chunk.chunk_fingerprint for chunk in chunks],
            "output_profile": accepted.output.to_wire(),
            "source_currentness_claim": currentness.to_wire(),
            "limits": profile.limits.to_wire(),
            "emits_audio_stream": emits_audio,
            "unavailable_disposition": "renderer_unqualified",
            # GUARD: as in `RenderPlanV1.to_wire`, only a plan with the table keys on it.
            **({"clip_audio": [row.to_wire() for row in clip_audio]} if clip_audio else {}),
        }
    )
    return RenderPlanV1(
        schema_version=RENDER_PLAN_SCHEMA,
        snapshot_revision=accepted.timeline_revision,
        snapshot_fingerprint=accepted.public_fingerprint,
        source_bindings=source_manifest.facts,
        source_manifest_fingerprint=source_manifest.manifest_fingerprint,
        font_manifest_fingerprint=font_facts.manifest_fingerprint,
        font_package_fingerprint=font_facts.package_fingerprint,
        font_facts_fingerprint=font_facts.facts_fingerprint,
        renderer_capability_profile_fingerprint=profile.profile_fingerprint,
        resolved_operations=chunks,
        output_profile=accepted.output,
        idempotency_fingerprint=idempotency_fingerprint,
        source_currentness_claim=currentness,
        limits=profile.limits,
        emits_audio_stream=emits_audio,
        unavailable_disposition="renderer_unqualified",
        clip_audio=clip_audio,
    )


def revalidate_render_plan_currentness(
    *,
    plan: RenderPlanV1,
    confirmation: SourceCurrentnessConfirmation,
) -> None:
    """Fail closed when a before-read or before-publication confirmation changed."""

    if type(plan) is not RenderPlanV1:
        raise _reject("invalid_contract", "currentness revalidation requires a typed render plan")
    if type(confirmation) is not SourceCurrentnessConfirmation:
        raise _reject("invalid_contract", "currentness revalidation requires typed confirmation")
    if (
        confirmation.public_fingerprint != plan.snapshot_fingerprint
        or confirmation.timeline_revision != plan.snapshot_revision
        or confirmation.manifest_fingerprint != plan.source_manifest_fingerprint
    ):
        raise _reject("stale_snapshot", "render plan snapshot currentness changed")
    # CRITICAL: a plan handoff is not a lease. M25-18 must supply a freshly revalidated token set
    # before its first source read and before publication; copying this stored claim is not enough.
    if confirmation != plan.source_currentness_claim:
        raise _reject("source_replaced", "render plan source currentness changed")


__all__ = [
    "FONT_FACTS_SCHEMA",
    "PACKAGED_FONT_MANIFEST_SCHEMA",
    "PACKAGED_FONT_PROFILE_ID",
    "PRIVATE_SOURCE_FACTS_MANIFEST_SCHEMA",
    "RENDERER_PROFILE_ID",
    "RENDERER_PROFILE_SCHEMA",
    "RENDER_PLAN_SCHEMA",
    "RENDER_PRIMITIVE_IDS",
    "RENDERER_QUALIFICATION_ROW_IDS",
    "SOURCE_CURRENTNESS_SCHEMA",
    "FontTitleBindingFact",
    "PackagedFontFacts",
    "PrivateSourceFact",
    "PrivateSourceFactsManifest",
    "RenderPlanV1",
    "RenderPlannerError",
    "RendererCapabilityProfile",
    "RendererQualificationRow",
    "SourceCurrentnessConfirmation",
    "SourceCurrentnessToken",
    "packaged_font_facts_fingerprint",
    "plan_render",
    "private_source_manifest_fingerprint",
    "required_unqualified_renderer_profile",
    "revalidate_render_plan_currentness",
    "source_facts_fingerprint",
]
