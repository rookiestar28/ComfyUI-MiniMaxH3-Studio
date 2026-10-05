"""M25-57 empty-capable NLE authoring state and its non-empty V1 render boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import NoReturn

from .canonical import canonical_fingerprint
from .composition_contract import (
    AUDIO_EXTENSION_SCHEMA,
    ENGINE_PROFILE_ID,
    INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    MAX_ASSETS,
    MAX_BLOCKERS,
    MAX_CLIPS,
    MAX_EXTENT_FRAMES,
    MAX_LANDMARKS,
    MAX_TRACKS,
    OUTPUT_PROFILE_ID,
    PUBLIC_SNAPSHOT_SCHEMA,
    RENDER_JOB_STATES,
    RENDER_TERMINAL_REASONS,
    RENDER_TERMINAL_STATES,
    RENDER_VOCABULARY_SCHEMA,
    AudioExtension,
    CompositionBlocker,
    CompositionClip,
    CompositionTrack,
    OutputProfile,
    PublicAsset,
    PublicCompositionSnapshot,
    Rational,
    RenderVocabulary,
    _asset,
    _audio_extension,
    _blocker,
    _clip,
    _fingerprint,
    _identifier,
    _integer,
    _mapping,
    _reject_private_recursive,
    _selected_capability,
    _track,
    _tuple,
    composition_contract_fingerprint,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_source_interval_coverage,
)
from .composition_contract import OPERATION_PROFILE_ID as V1_OPERATION_PROFILE_ID
from .errors import ContractValidationError

NLE_AUTHORING_SCHEMA = "h3.context.nle_authoring_state.v1"
NLE_AUTHORING_PROFILE_ID = "h3.authoring.nle_content_extent.v1"
NLE_OPERATION_PROFILE_ID = "h3.authoring.nle_operation.v2"
TIMELINE_TRANSACTION_SCHEMA_V2 = "h3.context.timeline_transaction.v2"
TIMELINE_RECEIPT_SCHEMA_V2 = "h3.context.timeline_receipt.v2"
TIMELINE_HISTORY_CURSOR_SCHEMA_V2 = "h3.context.timeline_history_cursor.v2"
TIMELINE_HISTORY_PROJECTION_SCHEMA_V2 = "h3.context.timeline_history_projection.v2"
EDIT_CAPACITY_FRAMES = 3_600

_STATE_KEYS = (
    "schema",
    "profile_id",
    "operation_profile_id",
    "project_id",
    "workspace_handle",
    "workspace_revision",
    "workspace_fingerprint",
    "timeline_revision",
    "timeline_fingerprint",
    "authoring_fingerprint",
    "edit_capacity_frames",
    "content_end_exclusive",
    "assets",
    "tracks",
    "clips",
    "audio_extension",
    "blockers",
)
_FP = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ZERO_FP = "sha256:" + "0" * 64


class NleAuthoringContractError(ContractValidationError):
    """Stable, content-free refusal from the versioned NLE authoring boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject(code: str, message: str) -> NoReturn:
    raise NleAuthoringContractError(code, message)


def _content_end(clips: tuple[CompositionClip, ...]) -> int:
    return max((clip.start_frame + clip.duration_frames for clip in clips), default=0)


def _state_timeline_fingerprint(
    *,
    edit_capacity_frames: int,
    content_end_exclusive: int,
    tracks: tuple[CompositionTrack, ...],
    clips: tuple[CompositionClip, ...],
    audio_extension: AudioExtension,
) -> str:
    return composition_contract_fingerprint(
        {
            "operation_profile_id": NLE_OPERATION_PROFILE_ID,
            "edit_capacity_frames": edit_capacity_frames,
            "content_end_exclusive": content_end_exclusive,
            "tracks": [track.to_wire() for track in tracks],
            "clips": [clip.to_wire() for clip in clips],
            "audio_extension": audio_extension.to_wire(),
        }
    )


def _workspace_fingerprint(
    *,
    project_id: str,
    workspace_handle: str,
    workspace_revision: int,
    timeline_revision: int,
    timeline_fingerprint: str,
) -> str:
    return canonical_fingerprint(
        {
            "project_id": project_id,
            "workspace_handle": workspace_handle,
            "workspace_revision": workspace_revision,
            "timeline_revision": timeline_revision,
            "timeline_fingerprint": timeline_fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class NleAuthoringState:
    schema: str
    profile_id: str
    operation_profile_id: str
    project_id: str
    workspace_handle: str
    workspace_revision: int
    workspace_fingerprint: str
    timeline_revision: int
    timeline_fingerprint: str
    authoring_fingerprint: str
    edit_capacity_frames: int
    content_end_exclusive: int
    assets: tuple[PublicAsset, ...]
    tracks: tuple[CompositionTrack, ...]
    clips: tuple[CompositionClip, ...]
    audio_extension: AudioExtension
    blockers: tuple[CompositionBlocker, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "operation_profile_id": self.operation_profile_id,
            "project_id": self.project_id,
            "workspace_handle": self.workspace_handle,
            "workspace_revision": self.workspace_revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "timeline_revision": self.timeline_revision,
            "timeline_fingerprint": self.timeline_fingerprint,
            "authoring_fingerprint": self.authoring_fingerprint,
            "edit_capacity_frames": self.edit_capacity_frames,
            "content_end_exclusive": self.content_end_exclusive,
            "assets": [asset.to_wire() for asset in self.assets],
            "tracks": [track.to_wire() for track in self.tracks],
            "clips": [clip.to_wire() for clip in self.clips],
            "audio_extension": self.audio_extension.to_wire(),
            "blockers": [blocker.to_wire() for blocker in self.blockers],
        }


def _validate_empty_material(
    *,
    assets: tuple[PublicAsset, ...],
    tracks: tuple[CompositionTrack, ...],
) -> None:
    if len({asset.asset_id for asset in assets}) != len(assets):
        _reject("invalid_contract", "asset identifiers must be unique")
    if len({track.track_id for track in tracks}) != len(tracks):
        _reject("invalid_contract", "track identifiers must be unique")
    if len({track.order for track in tracks}) != len(tracks):
        _reject("invalid_contract", "track order must be unique")
    if sum(track.kind == "primary_video" for track in tracks) != 1:
        _reject("invalid_contract", "exactly one primary video track is required")
    if tracks != tuple(sorted(tracks, key=lambda track: (track.order, track.track_id))):
        _reject("invalid_contract", "tracks are not in canonical order")


def _authoring_material_fingerprint(state: NleAuthoringState) -> str:
    material = state.to_wire()
    material.pop("authoring_fingerprint")
    return composition_contract_fingerprint(material)


def create_nle_authoring_state(
    *,
    project_id: str,
    workspace_handle: str,
    workspace_revision: int,
    timeline_revision: int,
    edit_capacity_frames: int,
    assets: tuple[PublicAsset, ...],
    tracks: tuple[CompositionTrack, ...],
    clips: tuple[CompositionClip, ...],
    audio_extension: AudioExtension,
    blockers: tuple[CompositionBlocker, ...] = (),
) -> NleAuthoringState:
    """Create a canonical V2 authoring state from backend-owned typed material."""

    canonical_tracks = tuple(sorted(tracks, key=lambda track: (track.order, track.track_id)))
    track_order = {track.track_id: track.order for track in canonical_tracks}
    canonical_clips = tuple(
        sorted(
            clips,
            key=lambda clip: (
                track_order.get(clip.track_id, MAX_TRACKS),
                clip.start_frame,
                clip.clip_id,
            ),
        )
    )
    content_end = _content_end(canonical_clips)
    timeline_fingerprint = _state_timeline_fingerprint(
        edit_capacity_frames=edit_capacity_frames,
        content_end_exclusive=content_end,
        tracks=canonical_tracks,
        clips=canonical_clips,
        audio_extension=audio_extension,
    )
    provisional = NleAuthoringState(
        NLE_AUTHORING_SCHEMA,
        NLE_AUTHORING_PROFILE_ID,
        NLE_OPERATION_PROFILE_ID,
        project_id,
        workspace_handle,
        workspace_revision,
        _workspace_fingerprint(
            project_id=project_id,
            workspace_handle=workspace_handle,
            workspace_revision=workspace_revision,
            timeline_revision=timeline_revision,
            timeline_fingerprint=timeline_fingerprint,
        ),
        timeline_revision,
        timeline_fingerprint,
        _ZERO_FP,
        edit_capacity_frames,
        content_end,
        assets,
        canonical_tracks,
        canonical_clips,
        audio_extension,
        blockers,
    )
    state = replace(provisional, authoring_fingerprint=_authoring_material_fingerprint(provisional))
    return decode_nle_authoring_state(state.to_wire())


def adapt_legacy_authoring_projection_to_nle_state(
    projection: Mapping[str, object],
    *,
    project_id: str,
    workspace_revision: int,
    asset_timings: Mapping[str, object],
) -> NleAuthoringState:
    """Adapt the server-owned legacy authoring projection without inventing an output profile."""

    projection_wire = _mapping(
        projection,
        (
            "schema",
            "workspace_handle",
            "context_source_id",
            "task_mode",
            "registry_fingerprint",
            "reference",
            "availability",
            "timeline",
            "rejection",
        ),
        "legacy_projection",
    )
    _reject_private_recursive(projection_wire, "legacy_projection")
    if (
        projection_wire["schema"] != "h3.context.authoring_workbench.projection.v1"
        or projection_wire["rejection"] is not None
    ):
        _reject("unsupported_profile", "legacy authoring projection is not accepted")
    if not isinstance(projection_wire["reference"], Mapping) or not isinstance(
        projection_wire["availability"], Mapping
    ):
        _reject("invalid_contract", "legacy authoring sections are invalid")
    timeline = _mapping(
        projection_wire["timeline"],
        (
            "revision",
            "content_fingerprint",
            "profile",
            "clips",
            "links",
            "selection",
            "blockers",
        ),
        "legacy_projection.timeline",
    )
    profile = timeline["profile"]
    clips_wire = timeline["clips"]
    links = timeline["links"]
    blockers_wire = timeline["blockers"]
    if (
        not isinstance(profile, Mapping)
        or profile.get("max_extent_frames") != EDIT_CAPACITY_FRAMES
        or not isinstance(clips_wire, list)
        or not isinstance(links, list)
        or not isinstance(blockers_wire, list)
    ):
        _reject("unsupported_profile", "legacy timeline profile or arrays are unsupported")
    if links:
        _reject("audio_editing_deferred", "legacy audiovisual links are not supported")

    asset_rows = tuple(asset_timings.values())
    assets = tuple(_asset(row, index) for index, row in enumerate(asset_rows))
    assets_by_id = {asset.asset_id: asset for asset in assets}
    tracks = (CompositionTrack("legacy.primary_video", "primary_video", 0, True, False),)
    clips: list[CompositionClip] = []
    for index, value in enumerate(clips_wire):
        if not isinstance(value, Mapping):
            _reject("invalid_contract", "legacy clip is invalid")
        if (
            value.get("kind") != "video"
            or type(value.get("lane")) is not int
            or value.get("lane") != 0
            or value.get("envelope") != []
        ):
            _reject("audio_editing_deferred", "only lane-zero legacy video can be adapted")
        asset_id = _identifier(value.get("asset_id"), f"legacy.clips[{index}].asset_id")
        if asset_id not in assets_by_id:
            _reject("timing_unavailable", "placed source is not in the admitted asset catalog")
        clips.append(
            _clip(
                {
                    "clip_id": value.get("clip_id"),
                    "asset_id": asset_id,
                    "track_id": "legacy.primary_video",
                    "start_frame": value.get("start_frame"),
                    "duration_frames": value.get("frames"),
                    "source_start_frame": value.get("source_start_frame"),
                    "enabled": True,
                    "transform": {
                        "anchor_x_bp": 5_000,
                        "anchor_y_bp": 5_000,
                        "position_x_bp": 0,
                        "position_y_bp": 0,
                        "scale_x_bp": 10_000,
                        "scale_y_bp": 10_000,
                        "rotation_mdeg": 0,
                    },
                    "crop": {"left_bp": 0, "top_bp": 0, "right_bp": 0, "bottom_bp": 0},
                    "opacity_bp": 10_000,
                    "blend": "normal",
                    "text": None,
                    "transition": {"kind": "none", "duration_frames": 0},
                    "effect": {
                        "kind": "none",
                        "brightness_permille": 0,
                        "contrast_permille": 1_000,
                        "saturation_permille": 1_000,
                    },
                },
                index,
            )
        )
    blockers = tuple(_blocker(row, index) for index, row in enumerate(blockers_wire))
    audio_extension = AudioExtension(
        AUDIO_EXTENSION_SCHEMA,
        "none_v1",
        INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
        (),
        "unsupported",
        "unsupported",
        "EmbeddedAudioSpanV1",
        "none_v1",
        "audio_editing_deferred",
    )
    return create_nle_authoring_state(
        project_id=project_id,
        workspace_handle=_identifier(
            projection_wire["workspace_handle"], "legacy_projection.workspace_handle"
        ),
        workspace_revision=workspace_revision,
        timeline_revision=_integer(timeline["revision"], "legacy.timeline.revision", 0, 1_000_000),
        edit_capacity_frames=EDIT_CAPACITY_FRAMES,
        assets=assets,
        tracks=tracks,
        clips=tuple(clips),
        audio_extension=audio_extension,
        blockers=blockers,
    )


def _server_output_profile(duration_frames: int) -> OutputProfile:
    return OutputProfile(
        OUTPUT_PROFILE_ID,
        Rational(24, 1),
        Rational(1, 24),
        1920,
        1080,
        duration_frames,
        "mp4",
        "h264",
        "yuv420p",
        Rational(1, 1),
        "bt709_sdr_limited_v1",
        "primary_embedded_follow_video_v1",
        "aac",
        48_000,
        1,
        2_000,
        2_048,
    )


def _v1_render_fingerprints(
    *,
    project_id: str,
    workspace_handle: str,
    workspace_revision: int,
    timeline_revision: int,
    output: OutputProfile,
    tracks: tuple[CompositionTrack, ...],
    clips: tuple[CompositionClip, ...],
    audio_extension: AudioExtension,
) -> tuple[str, str]:
    timeline_fingerprint = canonical_fingerprint(
        {
            "operation_profile_id": V1_OPERATION_PROFILE_ID,
            "tracks": [track.to_wire() for track in tracks],
            "clips": [clip.to_wire() for clip in clips],
            "audio_policy": output.audio_policy,
            "audio_extension": audio_extension.to_wire(),
        }
    )
    workspace_fingerprint = _workspace_fingerprint(
        project_id=project_id,
        workspace_handle=workspace_handle,
        workspace_revision=workspace_revision,
        timeline_revision=timeline_revision,
        timeline_fingerprint=timeline_fingerprint,
    )
    return timeline_fingerprint, workspace_fingerprint


def materialize_render_snapshot(
    state: NleAuthoringState,
) -> PublicCompositionSnapshot | None:
    """Return the unchanged V1 render contract for positive content extent only."""

    if not isinstance(state, NleAuthoringState):
        _reject("invalid_contract", "render materialization requires typed authoring state")
    if state.content_end_exclusive == 0:
        return None
    output = _server_output_profile(state.content_end_exclusive)
    timeline_fingerprint, workspace_fingerprint = _v1_render_fingerprints(
        project_id=state.project_id,
        workspace_handle=state.workspace_handle,
        workspace_revision=state.workspace_revision,
        timeline_revision=state.timeline_revision,
        output=output,
        tracks=state.tracks,
        clips=state.clips,
        audio_extension=state.audio_extension,
    )
    vocabulary = RenderVocabulary(
        RENDER_VOCABULARY_SCHEMA,
        "h3.context.authoring_render_request.v1",
        "h3.context.authoring_render_job.v1",
        "h3.context.authoring_render_receipt.v1",
        RENDER_JOB_STATES,
        RENDER_TERMINAL_STATES,
        RENDER_TERMINAL_REASONS,
    )
    wire: dict[str, object] = {
        "schema": PUBLIC_SNAPSHOT_SCHEMA,
        "profile_id": ENGINE_PROFILE_ID,
        "operation_profile_id": V1_OPERATION_PROFILE_ID,
        "project_id": state.project_id,
        "workspace_handle": state.workspace_handle,
        "workspace_revision": state.workspace_revision,
        "workspace_fingerprint": workspace_fingerprint,
        "timeline_revision": state.timeline_revision,
        "timeline_fingerprint": timeline_fingerprint,
        "public_fingerprint": _ZERO_FP,
        "output": output.to_wire(),
        "capability": _selected_capability().to_wire(),
        "assets": [asset.to_wire() for asset in state.assets],
        "tracks": [track.to_wire() for track in state.tracks],
        "clips": [clip.to_wire() for clip in state.clips],
        "audio_extension": state.audio_extension.to_wire(),
        "blockers": [blocker.to_wire() for blocker in state.blockers],
        "render_vocabulary": vocabulary.to_wire(),
    }
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    asset_by_id = {asset.asset_id: asset for asset in snapshot.assets}
    for clip in snapshot.clips:
        asset = asset_by_id.get(clip.asset_id) if clip.asset_id is not None else None
        if asset is not None and asset.kind == "video":
            try:
                resolve_source_interval_coverage(
                    asset,
                    clip.source_start_frame,
                    clip.duration_frames,
                    snapshot.output.frame_rate,
                )
            except ContractValidationError:
                _reject("source_range_unavailable", "authoring clip exceeds admitted source timing")
    return snapshot


def decode_nle_authoring_state(value: object) -> NleAuthoringState:
    wire = _mapping(value, _STATE_KEYS, "nle_authoring_state")
    if (
        wire["schema"] != NLE_AUTHORING_SCHEMA
        or wire["profile_id"] != NLE_AUTHORING_PROFILE_ID
        or wire["operation_profile_id"] != NLE_OPERATION_PROFILE_ID
    ):
        _reject("unsupported_profile", "NLE authoring schema or profile is unsupported")
    project_id = _identifier(wire["project_id"], "authoring.project_id")
    workspace_handle = _identifier(wire["workspace_handle"], "authoring.workspace_handle")
    workspace_revision = _integer(
        wire["workspace_revision"], "authoring.workspace_revision", 0, MAX_EXTENT_FRAMES
    )
    timeline_revision = _integer(
        wire["timeline_revision"], "authoring.timeline_revision", 0, MAX_EXTENT_FRAMES
    )
    workspace_fingerprint = _fingerprint(
        wire["workspace_fingerprint"], "authoring.workspace_fingerprint"
    )
    timeline_fingerprint = _fingerprint(
        wire["timeline_fingerprint"], "authoring.timeline_fingerprint"
    )
    authoring_fingerprint = _fingerprint(
        wire["authoring_fingerprint"], "authoring.authoring_fingerprint"
    )
    capacity = _integer(wire["edit_capacity_frames"], "authoring.edit_capacity_frames", 1, 3_600)
    if capacity != EDIT_CAPACITY_FRAMES:
        _reject("unsupported_profile", "edit capacity differs from the server profile")
    assets_wire = _tuple(wire["assets"], "authoring.assets", MAX_ASSETS)
    track_rows = _tuple(wire["tracks"], "authoring.tracks", MAX_TRACKS)
    clip_rows = _tuple(wire["clips"], "authoring.clips", MAX_CLIPS)
    blocker_rows = _tuple(wire["blockers"], "authoring.blockers", MAX_BLOCKERS)
    assets = tuple(_asset(item, index) for index, item in enumerate(assets_wire))
    if sum(len(asset.landmarks) for asset in assets) > MAX_LANDMARKS:
        _reject("resource_limit", "authoring landmarks exceed the profile")
    tracks = tuple(_track(item, index) for index, item in enumerate(track_rows))
    clips = tuple(_clip(item, index) for index, item in enumerate(clip_rows))
    audio_extension = _audio_extension(wire["audio_extension"])
    blockers = tuple(_blocker(item, index) for index, item in enumerate(blocker_rows))
    derived_end = _content_end(clips)
    if derived_end > capacity:
        _reject("edit_capacity_exceeded", "content extent exceeds edit capacity")
    content_end = _integer(
        wire["content_end_exclusive"], "authoring.content_end_exclusive", 0, capacity
    )
    if content_end != derived_end:
        _reject("invalid_contract", "content extent does not match placed clips")
    if not clips:
        _validate_empty_material(assets=assets, tracks=tracks)
    track_order = {track.track_id: track.order for track in tracks}
    if clips != tuple(
        sorted(
            clips,
            key=lambda clip: (
                track_order.get(clip.track_id, MAX_TRACKS),
                clip.start_frame,
                clip.clip_id,
            ),
        )
    ):
        _reject("invalid_contract", "clips are not in canonical order")
    state = NleAuthoringState(
        NLE_AUTHORING_SCHEMA,
        NLE_AUTHORING_PROFILE_ID,
        NLE_OPERATION_PROFILE_ID,
        project_id,
        workspace_handle,
        workspace_revision,
        workspace_fingerprint,
        timeline_revision,
        timeline_fingerprint,
        authoring_fingerprint,
        capacity,
        content_end,
        assets,
        tracks,
        clips,
        audio_extension,
        blockers,
    )
    expected_timeline_fingerprint = _state_timeline_fingerprint(
        edit_capacity_frames=capacity,
        content_end_exclusive=content_end,
        tracks=tracks,
        clips=clips,
        audio_extension=audio_extension,
    )
    if timeline_fingerprint != expected_timeline_fingerprint:
        _reject("stale_authoring", "timeline fingerprint does not match authoring material")
    expected_workspace_fingerprint = _workspace_fingerprint(
        project_id=project_id,
        workspace_handle=workspace_handle,
        workspace_revision=workspace_revision,
        timeline_revision=timeline_revision,
        timeline_fingerprint=timeline_fingerprint,
    )
    if workspace_fingerprint != expected_workspace_fingerprint:
        _reject("stale_authoring", "workspace fingerprint does not match authoring revisions")
    if authoring_fingerprint != _authoring_material_fingerprint(state):
        _reject("stale_authoring", "authoring fingerprint does not match its fields")
    if state.to_wire() != value:
        _reject("invalid_contract", "authoring state is not in canonical typed shape")
    if content_end > 0:
        materialize_render_snapshot(state)
    return state


def decode_nle_authoring_state_json(value: str) -> NleAuthoringState:
    if not isinstance(value, str):
        _reject("invalid_contract", "authoring JSON must be text")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                _reject("invalid_contract", "authoring JSON contains a duplicate member")
            result[key] = item
        return result

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=closed_pairs,
            parse_constant=lambda _: _reject("invalid_contract", "authoring JSON is non-finite"),
        )
    except NleAuthoringContractError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise NleAuthoringContractError("invalid_contract", "authoring JSON is invalid") from exc
    return decode_nle_authoring_state(decoded)


__all__ = [
    "EDIT_CAPACITY_FRAMES",
    "NLE_AUTHORING_PROFILE_ID",
    "NLE_AUTHORING_SCHEMA",
    "NLE_OPERATION_PROFILE_ID",
    "NleAuthoringContractError",
    "NleAuthoringState",
    "TIMELINE_HISTORY_CURSOR_SCHEMA_V2",
    "TIMELINE_HISTORY_PROJECTION_SCHEMA_V2",
    "TIMELINE_RECEIPT_SCHEMA_V2",
    "TIMELINE_TRANSACTION_SCHEMA_V2",
    "create_nle_authoring_state",
    "adapt_legacy_authoring_projection_to_nle_state",
    "decode_nle_authoring_state",
    "decode_nle_authoring_state_json",
    "materialize_render_snapshot",
]
