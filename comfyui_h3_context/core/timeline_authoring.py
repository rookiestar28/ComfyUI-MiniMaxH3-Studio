"""M20-02: the timeline authoring domain and deterministic constraint engine.

The Production editor (`M20-03`) needs one pure place that owns interactive timeline authoring:
which clips exist, where they sit, how they trim/split/merge, which audio is linked to which
video, what snaps where, and what a command may never silently do.  Three decisions shape it:

* **The engine consumes accepted authorities; it never re-derives them.**  Admitted sources come
  in as a :func:`reference_view` over the accepted M20-00 reference-set state -- a timeline clip
  may reference only a stable admitted asset ID and cannot mutate selection, order, soundtrack
  ownership or capacity, which remain exclusively M20-00 responsibilities.  Temporal constants
  come in as a :func:`build_h3_timeline_profile` view over the accepted M20-01 temporal profile;
  lattice and audio-period math is consumed, not restated.

* **Commands are atomic and every receipt carries its own undo.**  Each command names the exact
  revision (and optionally the exact state fingerprint) it expects; a stale or replayed command
  is rejected with no partial state.  Receipts carry a typed inverse command bound to the
  produced revision, so a journal needs no hidden snapshots and no global clipboard authority.
  Split's inverse is an explicit merge; a merge must prove timeline *and* source contiguity, so
  undo never fabricates content.

* **Hard constraints reject; snap only proposes.**  :func:`snap_candidates` is a bounded,
  deterministic candidate policy over profile grid boundaries, clip boundaries and the playhead
  -- advisory only.  The hard constraints -- per-lane non-overlap, extent and source bounds, and
  the exact audio-period alignment a linked audiovisual pair requires -- are command-level
  rejections; an off-grid link or move is never silently parked or clamped.

This module is not a second planner: `full_reference_timeline`, `feasible_av_timeline_planner`
and `temporal_event_alignment` are one-shot joins over validated contracts, while this domain is
the revisioned interactive authoring authority that later feeds them.  It stays independent of
ComfyUI, React, media decoding, providers and hosts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum

from . import length
from .canonical import canonical_bytes, canonical_fingerprint
from .composition_contract import (
    CLIP_AUDIO_FADE_MAX_FRAMES,
    CLIP_AUDIO_GAIN_MAX_MB,
    CLIP_AUDIO_GAIN_MIN_MB,
    IDENTITY_CLIP_AUDIO,
    MAX_CLIPS,
    MAX_TRACKS,
    NLE_OPERATION_IDS,
    ClipAudio,
    CompositionContractError,
    PublicAsset,
    PublicCompositionSnapshot,
    Rational,
    decode_public_snapshot,
    public_snapshot_fingerprint,
    resolve_source_interval_coverage,
    resolve_source_landmark_after_elapsed,
    source_intervals_are_contiguous,
)
from .composition_contract import _clip as _decode_composition_clip
from .composition_contract import _track as _decode_composition_track
from .contracts import MediaKind
from .errors import ContractValidationError
from .nle_authoring_contract import NleAuthoringState, create_nle_authoring_state
from .reference_set_authoring import ReferenceSetState
from .temporal_profile import (
    CONTEXT_EXTENT_GRID_FRAMES,
    EXACT_AUDIO_PERIOD_FRAMES,
    TEMPORAL_PROFILE_SCHEMA,
    TemporalCapabilityProfile,
)

TIMELINE_AUTHORING_SCHEMA = "h3-context-timeline-authoring/1"

MAX_REVISION = 1_000_000
MAX_EXTENT_FRAMES_CEILING = 1_000_000
MAX_LANES_CEILING = 64
MAX_CLIPS_CEILING = 1_024
MAX_GROUP_CEILING = 256
MAX_ENVELOPE_POINTS_CEILING = 256
MAX_SELECTION_CEILING = 1_024
MAX_STRENGTH_PER_MILLE = 1_000
MAX_SNAP_CANDIDATES = 32

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


class TimelineAuthoringError(ContractValidationError):
    """Typed atomic rejection; ``code`` is machine-readable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject(code: str, message: str) -> TimelineAuthoringError:
    return TimelineAuthoringError(code, message)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject("invalid_identifier", f"{field_name} must be a bounded identifier")
    return value


def _fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise _reject("invalid_fingerprint", f"{field_name} must be a sha256 fingerprint")
    return value


def _bounded_int(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _reject("invalid_integer", f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise _reject("integer_bounds", f"{field_name} must be within [{minimum}, {maximum}]")
    return value


class CommandKind(str, Enum):
    ADD_CLIP = "add_clip"
    REMOVE_CLIP = "remove_clip"
    RESTORE_CLIP = "restore_clip"
    MOVE_CLIP = "move_clip"
    MOVE_GROUP = "move_group"
    TRIM_CLIP = "trim_clip"
    SPLIT_CLIP = "split_clip"
    MERGE_CLIPS = "merge_clips"
    LINK_CLIPS = "link_clips"
    UNLINK_CLIPS = "unlink_clips"
    SET_ENVELOPE = "set_envelope"
    SELECT_CLIPS = "select_clips"


class TrimEdge(str, Enum):
    START = "start"
    END = "end"


class SnapKind(str, Enum):
    """Candidate provenance; priority is CLIP_BOUNDARY, then GRID, then PLAYHEAD."""

    CLIP_BOUNDARY = "clip_boundary"
    GRID = "grid"
    PLAYHEAD = "playhead"


_SNAP_PRIORITY = {SnapKind.CLIP_BOUNDARY: 0, SnapKind.GRID: 1, SnapKind.PLAYHEAD: 2}

_CLIP_KINDS = (MediaKind.VIDEO, MediaKind.AUDIO)


class TimelineCommandError(ContractValidationError):
    """M25-11 closed command-codec rejection with a stable, content-free code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject_nle(code: str, message: str) -> TimelineCommandError:
    return TimelineCommandError(code, message)


class NleCommandKind(str, Enum):
    CREATE_TRACK = "create_track"
    REMOVE_TRACK = "remove_track"
    REORDER_TRACK = "reorder_track"
    SET_TRACK_ENABLED = "set_track_enabled"
    SET_TRACK_LOCKED = "set_track_locked"
    INSERT_ASSET_CLIP = "insert_asset_clip"
    INSERT_TITLE_CLIP = "insert_title_clip"
    REPLACE_CLIP_ASSET = "replace_clip_asset"
    REMOVE_CLIP = "remove_clip"
    MOVE_CLIP = "move_clip"
    MOVE_GROUP = "move_group"
    TRIM_CLIP = "trim_clip"
    SPLIT_CLIP = "split_clip"
    MERGE_CLIPS = "merge_clips"
    INSERT_RANGE = "insert_range"
    OVERWRITE_RANGE = "overwrite_range"
    RIPPLE_DELETE = "ripple_delete"
    RIPPLE_TRIM = "ripple_trim"
    ROLL_EDIT = "roll_edit"
    SLIP_CLIP = "slip_clip"
    SLIDE_CLIP = "slide_clip"
    SET_CLIP_ENABLED = "set_clip_enabled"
    SET_VISUAL_TRANSFORM = "set_visual_transform"
    SET_CROP = "set_crop"
    SET_OPACITY_BLEND = "set_opacity_blend"
    SET_TEXT_CONTENT = "set_text_content"
    SET_TEXT_STYLE = "set_text_style"
    SET_TRANSITION = "set_transition"
    SET_EFFECT = "set_effect"
    SET_CLIP_AUDIO = "set_clip_audio"
    SELECT_CLIPS = "select_clips"
    UNDO = "undo"
    REDO = "redo"
    REBASE_TRANSACTION = "rebase_transaction"


# IMPORTANT: this table is the public M25-11 command boundary. Adding a permissive fallback would
# silently turn an unknown or deferred-audio intent into accepted history.
_NLE_PAYLOAD_KEYS: dict[NleCommandKind, tuple[str, ...]] = {
    NleCommandKind.CREATE_TRACK: ("track_id", "kind", "order"),
    NleCommandKind.REMOVE_TRACK: ("track_id",),
    NleCommandKind.REORDER_TRACK: ("track_id", "order"),
    NleCommandKind.SET_TRACK_ENABLED: ("track_id", "enabled"),
    NleCommandKind.SET_TRACK_LOCKED: ("track_id", "locked"),
    NleCommandKind.INSERT_ASSET_CLIP: ("clip",),
    NleCommandKind.INSERT_TITLE_CLIP: ("clip",),
    NleCommandKind.REPLACE_CLIP_ASSET: ("clip_id", "asset_id", "source_start_frame"),
    NleCommandKind.REMOVE_CLIP: ("clip_id",),
    NleCommandKind.MOVE_CLIP: ("clip_id", "delta_frames", "target_track_id"),
    NleCommandKind.MOVE_GROUP: ("clip_ids", "delta_frames", "target_track_ids"),
    NleCommandKind.TRIM_CLIP: ("clip_id", "edge", "delta_frames"),
    NleCommandKind.SPLIT_CLIP: ("clip_id", "at_offset_frames", "right_clip_id"),
    NleCommandKind.MERGE_CLIPS: ("left_clip_id", "right_clip_id"),
    NleCommandKind.INSERT_RANGE: ("clip", "scope_track_ids"),
    NleCommandKind.OVERWRITE_RANGE: (
        "clip",
        "start_frame",
        "duration_frames",
        "scope_track_ids",
        "remainder_ids",
    ),
    NleCommandKind.RIPPLE_DELETE: (
        "start_frame",
        "duration_frames",
        "scope_track_ids",
        "remainder_ids",
    ),
    NleCommandKind.RIPPLE_TRIM: ("clip_id", "edge", "delta_frames", "scope_track_ids"),
    NleCommandKind.ROLL_EDIT: ("left_clip_id", "right_clip_id", "delta_frames"),
    NleCommandKind.SLIP_CLIP: ("clip_id", "delta_frames"),
    NleCommandKind.SLIDE_CLIP: (
        "clip_id",
        "left_clip_id",
        "right_clip_id",
        "delta_frames",
    ),
    NleCommandKind.SET_CLIP_ENABLED: ("clip_id", "enabled"),
    NleCommandKind.SET_VISUAL_TRANSFORM: ("clip_id", "transform"),
    NleCommandKind.SET_CROP: ("clip_id", "crop"),
    NleCommandKind.SET_OPACITY_BLEND: ("clip_id", "opacity_bp", "blend"),
    NleCommandKind.SET_TEXT_CONTENT: ("clip_id", "content"),
    NleCommandKind.SET_TEXT_STYLE: ("clip_id", "style"),
    NleCommandKind.SET_TRANSITION: ("clip_id", "transition"),
    NleCommandKind.SET_EFFECT: ("clip_id", "effect"),
    NleCommandKind.SET_CLIP_AUDIO: (
        "clip_id",
        "gain_mb",
        "muted",
        "fade_in_frames",
        "fade_out_frames",
    ),
    NleCommandKind.SELECT_CLIPS: ("clip_ids",),
    NleCommandKind.UNDO: ("history_cursor",),
    NleCommandKind.REDO: ("history_cursor",),
    NleCommandKind.REBASE_TRANSACTION: ("base_timeline_fingerprint", "commands"),
}

_DEFERRED_AUDIO_COMMANDS = frozenset(
    {
        "create_audio_track",
        "insert_audio",
        "import_audio",
        "replace_audio",
        "link_audio",
        "unlink_audio",
        "set_gain",
        "set_pan",
        "set_mute",
        "set_solo",
        "set_envelope",
        "set_waveform",
        "mix_audio",
    }
)


@dataclass(frozen=True, slots=True, init=False)
class NleCommand:
    kind: NleCommandKind
    _payload_json: str

    def __init__(self, kind: NleCommandKind, payload: Mapping[str, object]) -> None:
        if (
            not isinstance(kind, NleCommandKind)
            or not isinstance(payload, Mapping)
            or set(payload) != set(_NLE_PAYLOAD_KEYS[kind])
        ):
            raise _reject_nle("invalid_command", "typed command members do not match its kind")
        try:
            payload_json = canonical_bytes(_canonical_nle_payload(kind, payload)).decode("utf-8")
        except ContractValidationError as exc:
            raise _reject_nle(
                "invalid_command", "command payload is not bounded canonical JSON"
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_payload_json", payload_json)

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._payload_json)
        if not isinstance(value, dict):
            raise AssertionError("validated command payload lost its object shape")
        return value

    def to_wire(self) -> dict[str, object]:
        return {"kind": self.kind.value, "payload": self.payload}


def decode_nle_command(value: object) -> NleCommand:
    if not isinstance(value, Mapping) or set(value) != {"kind", "payload"}:
        raise _reject_nle("invalid_command", "command must contain exactly kind and payload")
    raw_kind = value["kind"]
    if not isinstance(raw_kind, str):
        raise _reject_nle("invalid_command", "command kind must be a string")
    if raw_kind in _DEFERRED_AUDIO_COMMANDS or raw_kind.startswith("h3.authoring.audio.command.v1"):
        raise _reject_nle("audio_editing_deferred", "independent audio commands are unavailable")
    try:
        kind = NleCommandKind(raw_kind)
    except ValueError as exc:
        raise _reject_nle("invalid_command", "command kind is outside the closed profile") from exc
    payload = value["payload"]
    if not isinstance(payload, Mapping) or set(payload) != set(_NLE_PAYLOAD_KEYS[kind]):
        raise _reject_nle("invalid_command", "command payload members do not match its kind")
    return NleCommand(kind, payload)


if tuple(member.value for member in NleCommandKind) != NLE_OPERATION_IDS:
    raise RuntimeError("M25-11 command membership drifted from the accepted M25-10 profile")


@dataclass(frozen=True, slots=True)
class NleCommandTransition:
    snapshot: PublicCompositionSnapshot | NleAuthoringState
    selection: tuple[str, ...]
    affected_ids: tuple[str, ...]
    semantic_mutation: bool


def _nle_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise _reject_nle("invalid_command", f"{name} must be a bounded identifier")
    return value


def _nle_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _reject_nle("invalid_command", f"{name} is outside its integer bounds")
    return value


def _nle_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise _reject_nle("invalid_command", f"{name} must be a boolean")
    return value


def _nle_ids(value: object, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise _reject_nle("invalid_command", f"{name} must be a bounded identifier list")
    return tuple(sorted({_nle_id(member, f"{name}[]") for member in value}))


def _canonical_nle_payload(
    kind: NleCommandKind, payload: Mapping[str, object]
) -> dict[str, object]:
    normalized = dict(payload)
    if kind is NleCommandKind.SELECT_CLIPS:
        normalized["clip_ids"] = list(_nle_ids(payload["clip_ids"], "clip_ids", MAX_CLIPS))
    elif kind is NleCommandKind.MOVE_GROUP:
        clip_ids = _nle_ordered_ids(payload["clip_ids"], "clip_ids", 32)
        target_track_ids = _nle_ordered_ids(
            payload["target_track_ids"], "target_track_ids", 32, unique=False
        )
        if len(clip_ids) != len(target_track_ids):
            raise _reject_nle("invalid_command", "group clips and target tracks must align")
        pairs = sorted(zip(clip_ids, target_track_ids, strict=True))
        normalized["clip_ids"] = [pair[0] for pair in pairs]
        normalized["target_track_ids"] = [pair[1] for pair in pairs]
    elif kind in {
        NleCommandKind.INSERT_RANGE,
        NleCommandKind.OVERWRITE_RANGE,
        NleCommandKind.RIPPLE_DELETE,
        NleCommandKind.RIPPLE_TRIM,
    }:
        scope_track_ids = sorted(
            _nle_ordered_ids(payload["scope_track_ids"], "scope_track_ids", MAX_TRACKS)
        )
        normalized["scope_track_ids"] = scope_track_ids
    return normalized


def _nle_track(wire: dict[str, object], track_id: str) -> dict[str, object]:
    tracks = wire["tracks"]
    if not isinstance(tracks, list):
        raise AssertionError("validated snapshot lost its track list")
    for track in tracks:
        if isinstance(track, dict) and track.get("track_id") == track_id:
            return track
    raise _reject_nle("invalid_command", "command references an unknown track")


def _nle_clip(wire: dict[str, object], clip_id: str) -> dict[str, object]:
    clips = wire["clips"]
    if not isinstance(clips, list):
        raise AssertionError("validated snapshot lost its clip list")
    for clip in clips:
        if isinstance(clip, dict) and clip.get("clip_id") == clip_id:
            return clip
    raise _reject_nle("invalid_command", "command references an unknown clip")


def _require_track_editable(wire: dict[str, object], track_id: str) -> dict[str, object]:
    track = _nle_track(wire, track_id)
    if track.get("locked") is True:
        raise _reject_nle("invalid_command", "the owning track is locked")
    return track


def _require_clip_editable(wire: dict[str, object], clip_id: str) -> dict[str, object]:
    clip = _nle_clip(wire, clip_id)
    track_id = clip.get("track_id")
    if not isinstance(track_id, str):
        raise AssertionError("validated clip lost its track identifier")
    _require_track_editable(wire, track_id)
    return clip


def _canonicalize_nle_wire(
    wire: dict[str, object],
    *,
    authoring_base: NleAuthoringState | None = None,
) -> PublicCompositionSnapshot | NleAuthoringState:
    tracks = wire["tracks"]
    clips = wire["clips"]
    if not isinstance(tracks, list) or not isinstance(clips, list):
        raise AssertionError("validated snapshot lost canonical arrays")
    tracks.sort(key=lambda item: (item["order"], item["track_id"]))
    track_order = {item["track_id"]: item["order"] for item in tracks}
    clips.sort(
        key=lambda item: (
            track_order[item["track_id"]],
            item["start_frame"],
            item["clip_id"],
        )
    )
    if authoring_base is not None:
        return create_nle_authoring_state(
            project_id=authoring_base.project_id,
            workspace_handle=authoring_base.workspace_handle,
            workspace_revision=authoring_base.workspace_revision,
            timeline_revision=authoring_base.timeline_revision,
            edit_capacity_frames=authoring_base.edit_capacity_frames,
            assets=authoring_base.assets,
            tracks=tuple(
                _decode_composition_track(item, index) for index, item in enumerate(tracks)
            ),
            clips=tuple(_decode_composition_clip(item, index) for index, item in enumerate(clips)),
            audio_extension=authoring_base.audio_extension,
            blockers=authoring_base.blockers,
        )
    output = wire["output"]
    if not isinstance(output, dict):
        raise AssertionError("validated snapshot lost output")
    wire["timeline_fingerprint"] = canonical_fingerprint(
        {
            "operation_profile_id": wire["operation_profile_id"],
            "tracks": tracks,
            "clips": clips,
            "audio_policy": output["audio_policy"],
            "audio_extension": wire["audio_extension"],
        }
    )
    wire["workspace_fingerprint"] = canonical_fingerprint(
        {
            "project_id": wire["project_id"],
            "workspace_handle": wire["workspace_handle"],
            "workspace_revision": wire["workspace_revision"],
            "timeline_revision": wire["timeline_revision"],
            "timeline_fingerprint": wire["timeline_fingerprint"],
        }
    )
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    try:
        snapshot = decode_public_snapshot(wire)
        track_by_id = {track.track_id: track for track in snapshot.tracks}
        asset_by_id = {asset.asset_id: asset for asset in snapshot.assets}
        for clip in snapshot.clips:
            track = track_by_id[clip.track_id]
            if track.kind not in ("primary_video", "video_overlay") or clip.asset_id is None:
                continue
            # CRITICAL: source_start_frame is a source identity while duration_frames is output
            # time. Keep this guard on the shared rational PTS mapping or low/high-rate and VFR
            # edits are respectively rejected or admitted past the source end.
            resolve_source_interval_coverage(
                asset_by_id[clip.asset_id],
                clip.source_start_frame,
                clip.duration_frames,
                snapshot.output.frame_rate,
            )
        return snapshot
    except CompositionContractError as exc:
        raise _reject_nle(
            exc.code, "command result violates the accepted composition contract"
        ) from exc


def resign_nle_snapshot(
    snapshot: PublicCompositionSnapshot,
    *,
    workspace_revision: int,
    timeline_revision: int,
) -> PublicCompositionSnapshot:
    wire = snapshot.to_wire()
    wire["workspace_revision"] = _nle_int(workspace_revision, "workspace_revision", 0, MAX_REVISION)
    wire["timeline_revision"] = _nle_int(timeline_revision, "timeline_revision", 0, MAX_REVISION)
    result = _canonicalize_nle_wire(wire)
    if not isinstance(result, PublicCompositionSnapshot):
        raise AssertionError("V1 snapshot canonicalization returned a V2 authoring state")
    return result


def resign_nle_authoring_state(
    state: NleAuthoringState,
    *,
    workspace_revision: int,
    timeline_revision: int,
) -> NleAuthoringState:
    if not isinstance(state, NleAuthoringState):
        raise _reject_nle("invalid_command", "authoring history requires a typed V2 state")
    return create_nle_authoring_state(
        project_id=state.project_id,
        workspace_handle=state.workspace_handle,
        workspace_revision=_nle_int(workspace_revision, "workspace_revision", 0, MAX_REVISION),
        timeline_revision=_nle_int(timeline_revision, "timeline_revision", 0, MAX_REVISION),
        edit_capacity_frames=state.edit_capacity_frames,
        assets=state.assets,
        tracks=state.tracks,
        clips=state.clips,
        audio_extension=state.audio_extension,
        blockers=state.blockers,
    )


def _nle_frame_rate(snapshot: PublicCompositionSnapshot | NleAuthoringState) -> Rational:
    if isinstance(snapshot, NleAuthoringState):
        return Rational(24, 1)
    return snapshot.output.frame_rate


def _replace_track_order(tracks: list[object], track_id: str, order: int) -> None:
    typed = [item for item in tracks if isinstance(item, dict)]
    moving = next(item for item in typed if item.get("track_id") == track_id)
    typed.remove(moving)
    typed.insert(order, moving)
    for index, track in enumerate(typed):
        track["order"] = index
    tracks[:] = typed


def _nle_delta(value: object, name: str = "delta_frames") -> int:
    delta = _nle_int(value, name, -MAX_EXTENT_FRAMES_CEILING, MAX_EXTENT_FRAMES_CEILING)
    if delta == 0:
        raise _reject_nle("invalid_command", f"{name} must be non-zero")
    return delta


def _nle_ordered_ids(
    value: object,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
    unique: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum or (not value and not allow_empty):
        raise _reject_nle("invalid_command", f"{name} must be a bounded identifier list")
    members = tuple(_nle_id(member, f"{name}[]") for member in value)
    if unique and len(set(members)) != len(members):
        raise _reject_nle("invalid_command", f"{name} must contain unique identifiers")
    return members


def _nle_scope_tracks(wire: dict[str, object], value: object) -> tuple[str, ...]:
    scope = _nle_ordered_ids(value, "scope_track_ids", MAX_TRACKS)
    for track_id in scope:
        _require_track_editable(wire, track_id)
    return tuple(sorted(scope))


def _nle_track_accepts_clip(
    wire: dict[str, object], track_id: str, clip: dict[str, object]
) -> None:
    track = _require_track_editable(wire, track_id)
    kind = track.get("kind")
    is_text = clip.get("asset_id") is None and clip.get("text") is not None
    if (kind == "text_overlay") is not is_text:
        raise _reject_nle("invalid_command", "clip shape does not match the target track")
    if kind == "primary_video" and track_id != clip.get("track_id"):
        raise _reject_nle("invalid_command", "a clip cannot move into the primary track")


def _nle_is_video_clip(wire: dict[str, object], clip: dict[str, object]) -> bool:
    track_id = clip.get("track_id")
    if not isinstance(track_id, str):
        raise AssertionError("validated clip lost its track identifier")
    return _nle_track(wire, track_id).get("kind") in ("primary_video", "video_overlay")


def _nle_source_asset(
    snapshot: PublicCompositionSnapshot | NleAuthoringState, clip: dict[str, object]
) -> PublicAsset:
    asset_id = clip.get("asset_id")
    if not isinstance(asset_id, str):
        raise AssertionError("validated video clip lost its asset identifier")
    asset = next((item for item in snapshot.assets if item.asset_id == asset_id), None)
    if asset is None:
        raise AssertionError("validated video clip lost its admitted asset")
    return asset


def _nle_shift_source(
    snapshot: PublicCompositionSnapshot | NleAuthoringState,
    wire: dict[str, object],
    clip: dict[str, object],
    delta_frames: int,
) -> None:
    if _nle_is_video_clip(wire, clip):
        source_start = clip.get("source_start_frame")
        if not isinstance(source_start, int):
            raise AssertionError("validated clip lost its source start")
        # CRITICAL: delta_frames is output time while source_start_frame is a landmark identity.
        # Resolve through admitted rational timing; ordinal addition corrupts high/low-rate media.
        try:
            clip["source_start_frame"] = resolve_source_landmark_after_elapsed(
                _nle_source_asset(snapshot, clip),
                source_start,
                delta_frames,
                _nle_frame_rate(snapshot),
            )
        except CompositionContractError as exc:
            raise _reject_nle(exc.code, "source shift is not exactly representable") from exc


def _nle_clip_extent(clip: dict[str, object]) -> tuple[int, int]:
    start = clip.get("start_frame")
    duration = clip.get("duration_frames")
    # CRITICAL: public insert commands carry an untrusted nested clip before the complete snapshot
    # decoder runs. Reject missing/bool geometry here so range math and canonical sorting cannot
    # leak KeyError/AssertionError through the route instead of the closed command rejection.
    if type(start) is not int or type(duration) is not int:
        raise _reject_nle("invalid_command", "clip extent must use exact integer frames")
    return start, start + duration


def _nle_clip_audio(
    snapshot: PublicCompositionSnapshot | NleAuthoringState, clip_id: str
) -> ClipAudio:
    clip = next((item for item in snapshot.clips if item.clip_id == clip_id), None)
    if clip is None:
        raise AssertionError("validated clip lost its accepted audio member")
    return clip.audio


def _nle_write_clip_audio(clip: dict[str, object], audio: ClipAudio) -> None:
    # One value, one wire (`ClipAudio`): the identity is written absent.
    if audio == IDENTITY_CLIP_AUDIO:
        clip.pop("audio", None)
    else:
        clip["audio"] = audio.to_wire()


def _nle_fit_clip_audio(
    clip: dict[str, object],
    audio: ClipAudio,
    *,
    head_cut: bool = False,
    tail_cut: bool = False,
) -> None:
    """Write a clip's audio member fitted to the clip's new extent.

    A cut through the clip's head removes the material its fade-in ramped, and a cut through its
    tail the fade-out's, so that fade is dropped. A moved edge keeps its fade, and the fades left
    are clamped to the duration, fade-out first: no command that changes a duration is refused
    because of a fade.
    """

    start, end = _nle_clip_extent(clip)
    # A command that empties the clip is refused by the snapshot decoder with its own code.
    duration = max(end - start, 0)
    fade_out = 0 if tail_cut else min(audio.fade_out_frames, duration)
    fade_in = 0 if head_cut else min(audio.fade_in_frames, duration - fade_out)
    _nle_write_clip_audio(clip, replace(audio, fade_in_frames=fade_in, fade_out_frames=fade_out))


def _nle_prune_selection(
    snapshot: PublicCompositionSnapshot | NleAuthoringState,
    selection: tuple[str, ...],
) -> tuple[str, ...]:
    # CRITICAL: range commands can remove clips indirectly. Never publish a receipt whose
    # selection names a clip absent from the newly accepted snapshot.
    known = {clip.clip_id for clip in snapshot.clips}
    return tuple(clip_id for clip_id in selection if clip_id in known)


def _nle_new_clip(
    wire: dict[str, object], candidate: object, *, expected_id_absent: bool = True
) -> tuple[dict[str, object], str, str]:
    if not isinstance(candidate, dict):
        raise _reject_nle("invalid_command", "clip must be an object")
    clip_id = _nle_id(candidate.get("clip_id"), "clip.clip_id")
    track_id = _nle_id(candidate.get("track_id"), "clip.track_id")
    clips = wire["clips"]
    if not isinstance(clips, list):
        raise AssertionError("validated snapshot lost its clip list")
    if expected_id_absent and any(
        isinstance(item, dict) and item.get("clip_id") == clip_id for item in clips
    ):
        raise _reject_nle("invalid_contract", "clip identifier already exists")
    _nle_clip_extent(candidate)
    _nle_track_accepts_clip(wire, track_id, candidate)
    return candidate, clip_id, track_id


def _nle_remainder_ids(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise _reject_nle("invalid_command", "remainder_ids must be a bounded object")
    result = {
        _nle_id(key, "remainder_ids key"): _nle_id(member, "remainder_ids value")
        for key, member in value.items()
    }
    if len(set(result.values())) != len(result):
        raise _reject_nle("invalid_command", "remainder identifiers must be unique")
    return result


def _nle_delete_range(
    snapshot: PublicCompositionSnapshot | NleAuthoringState,
    wire: dict[str, object],
    *,
    start_frame: int,
    duration_frames: int,
    scope_track_ids: tuple[str, ...],
    remainder_ids: dict[str, str],
    ripple: bool,
) -> tuple[str, ...]:
    # CRITICAL: a clip spanning both range edges needs an explicit new ID for its right remainder.
    # Never silently discard that remainder or reuse an old ID during overwrite/ripple operations.
    clips = wire["clips"]
    if not isinstance(clips, list):
        raise AssertionError("validated snapshot lost its clip list")
    end_frame = start_frame + duration_frames
    existing_ids = {
        str(item["clip_id"])
        for item in clips
        if isinstance(item, dict) and isinstance(item.get("clip_id"), str)
    }
    affected: set[str] = set()
    required_remainders: set[str] = set()
    additions: list[dict[str, object]] = []
    removals: list[dict[str, object]] = []
    scope = set(scope_track_ids)
    for item in clips:
        if not isinstance(item, dict) or item.get("track_id") not in scope:
            continue
        clip_id = str(item["clip_id"])
        clip_start, clip_end = _nle_clip_extent(item)
        if clip_end <= start_frame:
            continue
        if clip_start >= end_frame:
            if ripple:
                item["start_frame"] = clip_start - duration_frames
                affected.add(clip_id)
            continue
        affected.add(clip_id)
        if clip_start < start_frame and clip_end > end_frame:
            required_remainders.add(clip_id)
            remainder_id = remainder_ids.get(clip_id)
            if remainder_id is None:
                continue
            if remainder_id in existing_ids:
                raise _reject_nle("invalid_contract", "remainder clip identifier already exists")
            right = json.loads(canonical_bytes(item))
            if not isinstance(right, dict):
                raise AssertionError("canonical clip copy lost its object shape")
            right["clip_id"] = remainder_id
            right["start_frame"] = start_frame if ripple else end_frame
            right["duration_frames"] = clip_end - end_frame
            _nle_shift_source(snapshot, wire, right, end_frame - clip_start)
            right["transition"] = {"kind": "none", "duration_frames": 0}
            item["duration_frames"] = start_frame - clip_start
            audio = _nle_clip_audio(snapshot, clip_id)
            _nle_fit_clip_audio(item, audio, tail_cut=True)
            _nle_fit_clip_audio(right, audio, head_cut=True)
            additions.append(right)
            existing_ids.add(remainder_id)
            affected.add(remainder_id)
        elif clip_start < start_frame:
            item["duration_frames"] = start_frame - clip_start
            _nle_fit_clip_audio(item, _nle_clip_audio(snapshot, clip_id), tail_cut=True)
        elif clip_end > end_frame:
            item["start_frame"] = start_frame if ripple else end_frame
            item["duration_frames"] = clip_end - end_frame
            _nle_shift_source(snapshot, wire, item, end_frame - clip_start)
            item["transition"] = {"kind": "none", "duration_frames": 0}
            _nle_fit_clip_audio(item, _nle_clip_audio(snapshot, clip_id), head_cut=True)
        else:
            removals.append(item)
    if required_remainders != set(remainder_ids):
        raise _reject_nle(
            "invalid_command", "remainder_ids must exactly name every split range clip"
        )
    for item in removals:
        clips.remove(item)
    clips.extend(additions)
    return tuple(sorted(affected))


def apply_nle_command(
    snapshot: PublicCompositionSnapshot | NleAuthoringState,
    command: NleCommand,
    *,
    selection: tuple[str, ...] = (),
) -> NleCommandTransition:
    """Apply one M25 command without advancing transaction revisions.

    The history owner advances revisions once after every command in the envelope succeeds.
    """

    if not isinstance(snapshot, (PublicCompositionSnapshot, NleAuthoringState)) or not isinstance(
        command, NleCommand
    ):
        raise _reject_nle("invalid_command", "command requires accepted typed inputs")
    wire = snapshot.to_wire()
    payload = command.payload
    kind = command.kind
    affected: tuple[str, ...]

    if kind is NleCommandKind.SELECT_CLIPS:
        selected = _nle_ids(payload["clip_ids"], "clip_ids", MAX_CLIPS)
        known = {clip.clip_id for clip in snapshot.clips}
        if any(clip_id not in known for clip_id in selected):
            raise _reject_nle("invalid_command", "selection references an unknown clip")
        return NleCommandTransition(snapshot, selected, selected, False)
    if kind in (NleCommandKind.UNDO, NleCommandKind.REDO, NleCommandKind.REBASE_TRANSACTION):
        raise _reject_nle("invalid_command", "history commands require the transaction owner")

    tracks = wire["tracks"]
    clips = wire["clips"]
    if not isinstance(tracks, list) or not isinstance(clips, list):
        raise AssertionError("validated snapshot lost canonical arrays")

    if kind is NleCommandKind.CREATE_TRACK:
        if len(tracks) >= MAX_TRACKS:
            raise _reject_nle("resource_limit", "track capacity is exhausted")
        track_id = _nle_id(payload["track_id"], "track_id")
        if any(isinstance(item, dict) and item.get("track_id") == track_id for item in tracks):
            raise _reject_nle("invalid_contract", "track identifier already exists")
        track_kind = payload["kind"]
        if track_kind not in ("video_overlay", "image_overlay", "text_overlay"):
            raise _reject_nle("invalid_command", "only overlay tracks can be added to a project")
        order = _nle_int(payload["order"], "order", 0, len(tracks))
        tracks.append(
            {
                "track_id": track_id,
                "kind": track_kind,
                "order": len(tracks),
                "enabled": True,
                "locked": False,
            }
        )
        _replace_track_order(tracks, track_id, order)
        affected = (track_id,)
    elif kind is NleCommandKind.REMOVE_TRACK:
        track_id = _nle_id(payload["track_id"], "track_id")
        track = _require_track_editable(wire, track_id)
        if track.get("kind") == "primary_video":
            raise _reject_nle("invalid_command", "the primary track cannot be removed")
        if any(isinstance(item, dict) and item.get("track_id") == track_id for item in clips):
            raise _reject_nle("invalid_command", "a non-empty track cannot be removed")
        tracks.remove(track)
        for index, item in enumerate(tracks):
            if isinstance(item, dict):
                item["order"] = index
        affected = (track_id,)
    elif kind is NleCommandKind.REORDER_TRACK:
        track_id = _nle_id(payload["track_id"], "track_id")
        _require_track_editable(wire, track_id)
        order = _nle_int(payload["order"], "order", 0, len(tracks) - 1)
        _replace_track_order(tracks, track_id, order)
        affected = (track_id,)
    elif kind in (NleCommandKind.SET_TRACK_ENABLED, NleCommandKind.SET_TRACK_LOCKED):
        track_id = _nle_id(payload["track_id"], "track_id")
        track = _nle_track(wire, track_id)
        member = "enabled" if kind is NleCommandKind.SET_TRACK_ENABLED else "locked"
        if member == "enabled" and track.get("locked") is True:
            raise _reject_nle("invalid_command", "the owning track is locked")
        track[member] = _nle_bool(payload[member], member)
        affected = (track_id,)
    elif kind in (NleCommandKind.INSERT_ASSET_CLIP, NleCommandKind.INSERT_TITLE_CLIP):
        if len(clips) >= MAX_CLIPS:
            raise _reject_nle("resource_limit", "clip capacity is exhausted")
        candidate, clip_id, track_id = _nle_new_clip(wire, payload["clip"])
        track = _nle_track(wire, track_id)
        is_title = kind is NleCommandKind.INSERT_TITLE_CLIP
        if (track.get("kind") == "text_overlay") is not is_title:
            raise _reject_nle("invalid_command", "clip insertion kind does not match its track")
        clips.append(candidate)
        affected = (clip_id, track_id)
    elif kind is NleCommandKind.REPLACE_CLIP_ASSET:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        if clip.get("asset_id") is None:
            raise _reject_nle("invalid_command", "title clips have no replaceable media asset")
        clip["asset_id"] = _nle_id(payload["asset_id"], "asset_id")
        clip["source_start_frame"] = _nle_int(
            payload["source_start_frame"], "source_start_frame", 0, MAX_EXTENT_FRAMES_CEILING
        )
        # The audio member scales the clip's own source's bound audio (`decode_public_snapshot`):
        # a replacement without bound audio drops the member instead of being refused for it.
        replacement = next(
            (item for item in snapshot.assets if item.asset_id == clip["asset_id"]), None
        )
        if replacement is None or replacement.embedded_audio != "present_bound":
            clip.pop("audio", None)
        affected = (clip_id, str(clip["asset_id"]))
    elif kind is NleCommandKind.REMOVE_CLIP:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        clips.remove(clip)
        selected = tuple(member for member in selection if member != clip_id)
        result = _canonicalize_nle_wire(
            wire,
            authoring_base=snapshot if isinstance(snapshot, NleAuthoringState) else None,
        )
        return NleCommandTransition(result, selected, (clip_id,), True)
    elif kind is NleCommandKind.MOVE_CLIP:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        delta = _nle_int(
            payload["delta_frames"],
            "delta_frames",
            -MAX_EXTENT_FRAMES_CEILING,
            MAX_EXTENT_FRAMES_CEILING,
        )
        target_track_id = _nle_id(payload["target_track_id"], "target_track_id")
        _nle_track_accepts_clip(wire, target_track_id, clip)
        # IMPORTANT: zero frame motion is valid when the track changes, just as the legacy
        # lane-based move allowed. Refuse only a move that changes neither axis.
        if delta == 0 and target_track_id == clip.get("track_id"):
            raise _reject_nle("invalid_command", "move must change time or track")
        start, _ = _nle_clip_extent(clip)
        clip["start_frame"] = start + delta
        clip["track_id"] = target_track_id
        affected = (clip_id, target_track_id)
    elif kind is NleCommandKind.MOVE_GROUP:
        clip_ids = _nle_ordered_ids(payload["clip_ids"], "clip_ids", 32)
        target_track_ids = _nle_ordered_ids(
            payload["target_track_ids"], "target_track_ids", 32, unique=False
        )
        if len(clip_ids) != len(target_track_ids):
            raise _reject_nle("invalid_command", "group clips and target tracks must align")
        delta = _nle_int(
            payload["delta_frames"],
            "delta_frames",
            -MAX_EXTENT_FRAMES_CEILING,
            MAX_EXTENT_FRAMES_CEILING,
        )
        moving: list[tuple[dict[str, object], str]] = []
        for clip_id, target_track_id in zip(clip_ids, target_track_ids, strict=True):
            clip = _require_clip_editable(wire, clip_id)
            _nle_track_accepts_clip(wire, target_track_id, clip)
            moving.append((clip, target_track_id))
        if delta == 0 and all(clip.get("track_id") == target for clip, target in moving):
            raise _reject_nle("invalid_command", "group move must change time or track")
        for clip, target_track_id in moving:
            start, _ = _nle_clip_extent(clip)
            clip["start_frame"] = start + delta
            clip["track_id"] = target_track_id
        affected = clip_ids + target_track_ids
    elif kind is NleCommandKind.TRIM_CLIP:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        edge = payload["edge"]
        if edge not in ("start", "end"):
            raise _reject_nle("invalid_command", "trim edge is unsupported")
        delta = _nle_delta(payload["delta_frames"])
        start, end = _nle_clip_extent(clip)
        if edge == "start":
            clip["start_frame"] = start + delta
            clip["duration_frames"] = end - (start + delta)
            _nle_shift_source(snapshot, wire, clip, delta)
            clip["transition"] = {"kind": "none", "duration_frames": 0}
        else:
            clip["duration_frames"] = end + delta - start
        _nle_fit_clip_audio(clip, _nle_clip_audio(snapshot, clip_id))
        affected = (clip_id,)
    elif kind is NleCommandKind.SPLIT_CLIP:
        if len(clips) >= MAX_CLIPS:
            raise _reject_nle("resource_limit", "clip capacity is exhausted")
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        _, clip_end = _nle_clip_extent(clip)
        duration = clip.get("duration_frames")
        if not isinstance(duration, int):
            raise AssertionError("validated clip lost its duration")
        offset = _nle_int(payload["at_offset_frames"], "at_offset_frames", 1, duration - 1)
        right_clip_id = _nle_id(payload["right_clip_id"], "right_clip_id")
        if any(isinstance(item, dict) and item.get("clip_id") == right_clip_id for item in clips):
            raise _reject_nle("invalid_contract", "right clip identifier already exists")
        right = json.loads(canonical_bytes(clip))
        if not isinstance(right, dict):
            raise AssertionError("canonical clip copy lost its object shape")
        start, _ = _nle_clip_extent(clip)
        clip["duration_frames"] = offset
        right["clip_id"] = right_clip_id
        right["start_frame"] = start + offset
        right["duration_frames"] = clip_end - (start + offset)
        _nle_shift_source(snapshot, wire, right, offset)
        right["transition"] = {"kind": "none", "duration_frames": 0}
        # Both halves are fitted from the clip's member before the split. The left half's written
        # member has already lost the fade-out the right half keeps.
        audio = _nle_clip_audio(snapshot, clip_id)
        _nle_fit_clip_audio(clip, audio, tail_cut=True)
        _nle_fit_clip_audio(right, audio, head_cut=True)
        clips.append(right)
        affected = (clip_id, right_clip_id)
    elif kind is NleCommandKind.MERGE_CLIPS:
        left_id = _nle_id(payload["left_clip_id"], "left_clip_id")
        right_id = _nle_id(payload["right_clip_id"], "right_clip_id")
        if left_id == right_id:
            raise _reject_nle("invalid_command", "merge requires two distinct clips")
        left = _require_clip_editable(wire, left_id)
        right = _require_clip_editable(wire, right_id)
        left_start, left_end = _nle_clip_extent(left)
        right_start, right_end = _nle_clip_extent(right)
        if left_end != right_start or left.get("track_id") != right.get("track_id"):
            raise _reject_nle("invalid_command", "merge clips must be timeline-contiguous")
        comparable = (
            "asset_id",
            "track_id",
            "enabled",
            "transform",
            "crop",
            "opacity_bp",
            "blend",
            "text",
            "effect",
        )
        if any(left.get(member) != right.get(member) for member in comparable):
            raise _reject_nle(
                "invalid_command", "merge clips must have identical content properties"
            )
        if right.get("transition") != {"kind": "none", "duration_frames": 0}:
            raise _reject_nle("invalid_command", "the right merge boundary must have no transition")
        # Merge admits only the halves a split makes (`SPLIT_CLIP`), so no fade is lost or invented
        # at the joined boundary.
        left_audio = _nle_clip_audio(snapshot, left_id)
        right_audio = _nle_clip_audio(snapshot, right_id)
        if (left_audio.gain_mb, left_audio.muted) != (right_audio.gain_mb, right_audio.muted):
            raise _reject_nle("invalid_command", "merge clips must have the same audio adjustment")
        if left_audio.fade_out_frames != 0 or right_audio.fade_in_frames != 0:
            raise _reject_nle("invalid_command", "merge clips must not fade at their shared edge")
        if _nle_is_video_clip(wire, left):
            left_source = left.get("source_start_frame")
            right_source = right.get("source_start_frame")
            if not isinstance(left_source, int) or not isinstance(right_source, int):
                raise AssertionError("validated clips lost source positions")
            # CRITICAL: merge continuity is physical source time, not source ordinal plus output
            # frames. The latter rejects valid high-rate edits and admits invalid low-rate joins.
            try:
                contiguous = source_intervals_are_contiguous(
                    _nle_source_asset(snapshot, left),
                    left_source,
                    left_end - left_start,
                    right_source,
                    _nle_frame_rate(snapshot),
                )
            except CompositionContractError as exc:
                raise _reject_nle(exc.code, "merge source timing is unavailable") from exc
            if not contiguous:
                raise _reject_nle("invalid_command", "merge clips must be source-contiguous")
        left["duration_frames"] = right_end - left_start
        _nle_write_clip_audio(
            left, replace(left_audio, fade_out_frames=right_audio.fade_out_frames)
        )
        clips.remove(right)
        selected = tuple(left_id if member == right_id else member for member in selection)
        selected = tuple(sorted(set(selected)))
        result = _canonicalize_nle_wire(
            wire,
            authoring_base=snapshot if isinstance(snapshot, NleAuthoringState) else None,
        )
        return NleCommandTransition(result, selected, (left_id, right_id), True)
    elif kind is NleCommandKind.INSERT_RANGE:
        if len(clips) >= MAX_CLIPS:
            raise _reject_nle("resource_limit", "clip capacity is exhausted")
        candidate, clip_id, track_id = _nle_new_clip(wire, payload["clip"])
        scope = _nle_scope_tracks(wire, payload["scope_track_ids"])
        if track_id not in scope:
            raise _reject_nle("invalid_command", "insert target must be inside its declared scope")
        start, end = _nle_clip_extent(candidate)
        duration = end - start
        changed: set[str] = {clip_id, track_id}
        for item in clips:
            if not isinstance(item, dict) or item.get("track_id") not in scope:
                continue
            item_start, item_end = _nle_clip_extent(item)
            if item_start < start < item_end:
                raise _reject_nle(
                    "invalid_command", "insert range cannot split undeclared material"
                )
            if item_start >= start:
                item["start_frame"] = item_start + duration
                changed.add(str(item["clip_id"]))
        clips.append(candidate)
        affected = tuple(changed)
    elif kind in (NleCommandKind.OVERWRITE_RANGE, NleCommandKind.RIPPLE_DELETE):
        original_clip_ids = {
            str(item["clip_id"])
            for item in clips
            if isinstance(item, dict) and isinstance(item.get("clip_id"), str)
        }
        start = _nle_int(payload["start_frame"], "start_frame", 0, MAX_EXTENT_FRAMES_CEILING - 1)
        duration = _nle_int(
            payload["duration_frames"], "duration_frames", 1, MAX_EXTENT_FRAMES_CEILING
        )
        if start + duration > MAX_EXTENT_FRAMES_CEILING:
            raise _reject_nle("invalid_command", "range exceeds the integer-frame domain")
        scope = _nle_scope_tracks(wire, payload["scope_track_ids"])
        remainders = _nle_remainder_ids(payload["remainder_ids"])
        range_affected = _nle_delete_range(
            snapshot,
            wire,
            start_frame=start,
            duration_frames=duration,
            scope_track_ids=scope,
            remainder_ids=remainders,
            ripple=kind is NleCommandKind.RIPPLE_DELETE,
        )
        if kind is NleCommandKind.OVERWRITE_RANGE:
            if len(clips) >= MAX_CLIPS:
                raise _reject_nle("resource_limit", "clip capacity is exhausted")
            candidate, clip_id, track_id = _nle_new_clip(wire, payload["clip"])
            if clip_id in original_clip_ids:
                raise _reject_nle("invalid_contract", "overwrite clip identifier is not new")
            candidate_start, candidate_end = _nle_clip_extent(candidate)
            if (candidate_start, candidate_end - candidate_start) != (start, duration):
                raise _reject_nle("invalid_command", "overwrite clip must exactly fill its range")
            if track_id not in scope:
                raise _reject_nle("invalid_command", "overwrite target must be inside its scope")
            clips.append(candidate)
            affected = range_affected + (clip_id, track_id)
        else:
            affected = range_affected
    elif kind is NleCommandKind.RIPPLE_TRIM:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        scope = _nle_scope_tracks(wire, payload["scope_track_ids"])
        if clip.get("track_id") not in scope:
            raise _reject_nle("invalid_command", "trim target must be inside its declared scope")
        edge = payload["edge"]
        if edge not in ("start", "end"):
            raise _reject_nle("invalid_command", "trim edge is unsupported")
        delta = _nle_delta(payload["delta_frames"])
        old_start, old_end = _nle_clip_extent(clip)
        ripple_changed: set[str] = {clip_id}
        if edge == "start":
            # CRITICAL: a ripple in-point trim closes/opens time after the edited clip; moving the
            # clip's sequence start itself is an ordinary trim and leaves an unintended gap.
            clip["duration_frames"] = old_end - old_start - delta
            _nle_shift_source(snapshot, wire, clip, delta)
            clip["transition"] = {"kind": "none", "duration_frames": 0}
            for item in clips:
                if isinstance(item, dict) and item is not clip and item.get("track_id") in scope:
                    item_start, _ = _nle_clip_extent(item)
                    if item_start >= old_end:
                        item["start_frame"] = item_start - delta
                        ripple_changed.add(str(item["clip_id"]))
        else:
            clip["duration_frames"] = old_end + delta - old_start
            for item in clips:
                if isinstance(item, dict) and item is not clip and item.get("track_id") in scope:
                    item_start, _ = _nle_clip_extent(item)
                    if item_start >= old_end:
                        item["start_frame"] = item_start + delta
                        ripple_changed.add(str(item["clip_id"]))
        _nle_fit_clip_audio(clip, _nle_clip_audio(snapshot, clip_id))
        affected = tuple(ripple_changed)
    elif kind is NleCommandKind.ROLL_EDIT:
        left_id = _nle_id(payload["left_clip_id"], "left_clip_id")
        right_id = _nle_id(payload["right_clip_id"], "right_clip_id")
        left = _require_clip_editable(wire, left_id)
        right = _require_clip_editable(wire, right_id)
        left_start, left_end = _nle_clip_extent(left)
        right_start, right_end = _nle_clip_extent(right)
        if left_end != right_start or left.get("track_id") != right.get("track_id"):
            raise _reject_nle("invalid_command", "roll requires adjacent clips on one track")
        delta = _nle_delta(payload["delta_frames"])
        left["duration_frames"] = left_end + delta - left_start
        right["start_frame"] = right_start + delta
        right["duration_frames"] = right_end - (right_start + delta)
        _nle_shift_source(snapshot, wire, right, delta)
        right["transition"] = {"kind": "none", "duration_frames": 0}
        _nle_fit_clip_audio(left, _nle_clip_audio(snapshot, left_id))
        _nle_fit_clip_audio(right, _nle_clip_audio(snapshot, right_id))
        affected = (left_id, right_id)
    elif kind is NleCommandKind.SLIP_CLIP:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        if not _nle_is_video_clip(wire, clip):
            raise _reject_nle("invalid_command", "only timed video media can slip")
        _nle_shift_source(snapshot, wire, clip, _nle_delta(payload["delta_frames"]))
        affected = (clip_id,)
    elif kind is NleCommandKind.SLIDE_CLIP:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        left_id = _nle_id(payload["left_clip_id"], "left_clip_id")
        right_id = _nle_id(payload["right_clip_id"], "right_clip_id")
        if len({clip_id, left_id, right_id}) != 3:
            raise _reject_nle("invalid_command", "slide requires three distinct clips")
        middle = _require_clip_editable(wire, clip_id)
        left = _require_clip_editable(wire, left_id)
        right = _require_clip_editable(wire, right_id)
        left_start, left_end = _nle_clip_extent(left)
        middle_start, middle_end = _nle_clip_extent(middle)
        right_start, right_end = _nle_clip_extent(right)
        if (
            len({left.get("track_id"), middle.get("track_id"), right.get("track_id")}) != 1
            or left_end != middle_start
            or middle_end != right_start
        ):
            raise _reject_nle("invalid_command", "slide requires adjacent clips on one track")
        delta = _nle_delta(payload["delta_frames"])
        left["duration_frames"] = left_end + delta - left_start
        middle["start_frame"] = middle_start + delta
        right["start_frame"] = right_start + delta
        right["duration_frames"] = right_end - (right_start + delta)
        _nle_shift_source(snapshot, wire, right, delta)
        right["transition"] = {"kind": "none", "duration_frames": 0}
        _nle_fit_clip_audio(left, _nle_clip_audio(snapshot, left_id))
        _nle_fit_clip_audio(right, _nle_clip_audio(snapshot, right_id))
        affected = (left_id, clip_id, right_id)
    elif kind is NleCommandKind.SET_CLIP_AUDIO:
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        audio = ClipAudio(
            _nle_int(payload["gain_mb"], "gain_mb", CLIP_AUDIO_GAIN_MIN_MB, CLIP_AUDIO_GAIN_MAX_MB),
            _nle_bool(payload["muted"], "muted"),
            _nle_int(payload["fade_in_frames"], "fade_in_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES),
            _nle_int(payload["fade_out_frames"], "fade_out_frames", 0, CLIP_AUDIO_FADE_MAX_FRAMES),
        )
        if audio != IDENTITY_CLIP_AUDIO:
            # The command's own refusals, ahead of the decoder's admission and sum rules, so a
            # request that could never be applied is refused as a command and not as a contract.
            source = next(
                (item for item in snapshot.assets if item.asset_id == clip.get("asset_id")), None
            )
            if source is None or source.kind != "video" or source.embedded_audio != "present_bound":
                raise _reject_nle(
                    "invalid_command", "only a clip whose source has bound audio is adjusted"
                )
            start, end = _nle_clip_extent(clip)
            if audio.fade_in_frames + audio.fade_out_frames > end - start:
                raise _reject_nle("invalid_command", "the fades exceed the clip duration")
        _nle_write_clip_audio(clip, audio)
        affected = (clip_id,)
    elif kind in (
        NleCommandKind.SET_CLIP_ENABLED,
        NleCommandKind.SET_VISUAL_TRANSFORM,
        NleCommandKind.SET_CROP,
        NleCommandKind.SET_OPACITY_BLEND,
        NleCommandKind.SET_TEXT_CONTENT,
        NleCommandKind.SET_TEXT_STYLE,
        NleCommandKind.SET_TRANSITION,
        NleCommandKind.SET_EFFECT,
    ):
        clip_id = _nle_id(payload["clip_id"], "clip_id")
        clip = _require_clip_editable(wire, clip_id)
        if kind is NleCommandKind.SET_CLIP_ENABLED:
            clip["enabled"] = _nle_bool(payload["enabled"], "enabled")
        elif kind is NleCommandKind.SET_VISUAL_TRANSFORM:
            clip["transform"] = payload["transform"]
        elif kind is NleCommandKind.SET_CROP:
            if clip.get("text") is not None:
                raise _reject_nle("invalid_command", "text clips do not accept crop commands")
            clip["crop"] = payload["crop"]
        elif kind is NleCommandKind.SET_OPACITY_BLEND:
            clip["opacity_bp"] = payload["opacity_bp"]
            clip["blend"] = payload["blend"]
        elif kind in (NleCommandKind.SET_TEXT_CONTENT, NleCommandKind.SET_TEXT_STYLE):
            text = clip.get("text")
            if not isinstance(text, dict):
                raise _reject_nle("invalid_command", "text command requires a title clip")
            if kind is NleCommandKind.SET_TEXT_CONTENT:
                text["content"] = payload["content"]
            else:
                style = payload["style"]
                if not isinstance(style, dict) or "content" in style:
                    raise _reject_nle("invalid_command", "text style must exclude content")
                content = text.get("content")
                text.clear()
                text.update({"content": content, **style})
        elif kind is NleCommandKind.SET_TRANSITION:
            clip["transition"] = payload["transition"]
        else:
            clip["effect"] = payload["effect"]
        affected = (clip_id,)
    else:
        raise _reject_nle("invalid_command", "command semantics are not implemented")

    result = _canonicalize_nle_wire(
        wire,
        authoring_base=snapshot if isinstance(snapshot, NleAuthoringState) else None,
    )
    return NleCommandTransition(
        result,
        _nle_prune_selection(result, selection),
        tuple(sorted(set(affected))),
        True,
    )


@dataclass(frozen=True, slots=True)
class TimelineProfileInput:
    """Frozen consumption of the accepted M20-01 temporal profile; never re-derived here."""

    schema: str
    version: int
    fingerprint: str
    video_fps: int
    frame_grid: int
    audio_period_frames: int
    max_extent_frames: int

    def __post_init__(self) -> None:
        if self.schema != TEMPORAL_PROFILE_SCHEMA:
            raise _reject("profile_schema", "profile schema is not the accepted temporal profile")
        _bounded_int(self.version, "version", 1, MAX_REVISION)
        _fingerprint(self.fingerprint, "fingerprint")
        _bounded_int(self.video_fps, "video_fps", 1, 1_000)
        _bounded_int(self.frame_grid, "frame_grid", 1, MAX_EXTENT_FRAMES_CEILING)
        _bounded_int(self.audio_period_frames, "audio_period_frames", 1, 1_000)
        _bounded_int(self.max_extent_frames, "max_extent_frames", 1, MAX_EXTENT_FRAMES_CEILING)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "video_fps": self.video_fps,
            "frame_grid": self.frame_grid,
            "audio_period_frames": self.audio_period_frames,
            "max_extent_frames": self.max_extent_frames,
        }


def build_h3_timeline_profile(profile: TemporalCapabilityProfile) -> TimelineProfileInput:
    """Freeze the accepted temporal profile into the authoring view this engine consumes."""

    if not isinstance(profile, TemporalCapabilityProfile):
        raise _reject("invalid_profile", "profile must be a TemporalCapabilityProfile")
    return TimelineProfileInput(
        schema=TEMPORAL_PROFILE_SCHEMA,
        version=profile.version,
        fingerprint=canonical_fingerprint(profile.to_wire()),
        video_fps=length.FPS,
        frame_grid=CONTEXT_EXTENT_GRID_FRAMES,
        audio_period_frames=EXACT_AUDIO_PERIOD_FRAMES,
        max_extent_frames=length.MAX_FRAME_COUNT,
    )


@dataclass(frozen=True, slots=True)
class TimelineAuthoringLimits:
    """Authoring ceilings; one validated input, reported rather than re-derived."""

    max_clips: int
    max_lanes: int
    max_group: int
    max_envelope_points: int
    max_selection: int

    def __post_init__(self) -> None:
        _bounded_int(self.max_clips, "max_clips", 1, MAX_CLIPS_CEILING)
        _bounded_int(self.max_lanes, "max_lanes", 1, MAX_LANES_CEILING)
        _bounded_int(self.max_group, "max_group", 1, MAX_GROUP_CEILING)
        _bounded_int(
            self.max_envelope_points, "max_envelope_points", 1, MAX_ENVELOPE_POINTS_CEILING
        )
        _bounded_int(self.max_selection, "max_selection", 1, MAX_SELECTION_CEILING)

    def to_wire(self) -> dict[str, int]:
        return {
            "max_clips": self.max_clips,
            "max_lanes": self.max_lanes,
            "max_group": self.max_group,
            "max_envelope_points": self.max_envelope_points,
            "max_selection": self.max_selection,
        }


def build_h3_timeline_limits() -> TimelineAuthoringLimits:
    return TimelineAuthoringLimits(
        max_clips=128,
        max_lanes=8,
        max_group=32,
        max_envelope_points=32,
        max_selection=128,
    )


@dataclass(frozen=True, slots=True)
class ReferencedAsset:
    """One admitted M20-00 source as the timeline is allowed to see it."""

    asset_id: str
    kind: MediaKind
    fingerprint: str
    duration_milliseconds: int | None

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "asset_id")
        if not isinstance(self.kind, MediaKind):
            raise _reject("invalid_kind", "kind must be a MediaKind")
        _fingerprint(self.fingerprint, "fingerprint")
        if self.duration_milliseconds is not None:
            _bounded_int(self.duration_milliseconds, "duration_milliseconds", 1, 86_400_000)


@dataclass(frozen=True, slots=True)
class TimelineReferenceView:
    """Producer-attributed view over the admitted reference set at one revision."""

    revision: int
    fingerprint: str
    assets: tuple[ReferencedAsset, ...]

    def __post_init__(self) -> None:
        _bounded_int(self.revision, "revision", 1, MAX_REVISION)
        _fingerprint(self.fingerprint, "fingerprint")
        seen: set[str] = set()
        for asset in self.assets:
            if not isinstance(asset, ReferencedAsset):
                raise _reject("invalid_asset", "assets must be ReferencedAsset values")
            if asset.asset_id in seen:
                raise _reject("duplicate_asset", "reference view repeats an asset id")
            seen.add(asset.asset_id)

    def asset(self, asset_id: str) -> ReferencedAsset | None:
        for candidate in self.assets:
            if candidate.asset_id == asset_id:
                return candidate
        return None


def reference_view(state: ReferenceSetState) -> TimelineReferenceView:
    """The one mapping from the accepted M20-00 state into what this engine may consult."""

    if not isinstance(state, ReferenceSetState):
        raise _reject("invalid_reference_state", "state must be a ReferenceSetState")
    assets = tuple(
        ReferencedAsset(
            asset_id=entry.source_id,
            kind=entry.kind,
            fingerprint=entry.fingerprint,
            duration_milliseconds=entry.duration_milliseconds,
        )
        for entry in state.images + state.videos + state.audios
    )
    return TimelineReferenceView(
        revision=state.revision,
        fingerprint=canonical_fingerprint(state.to_wire()),
        assets=assets,
    )


@dataclass(frozen=True, slots=True)
class EnvelopePoint:
    """One strength point at an exact frame offset inside its clip."""

    offset_frames: int
    strength_per_mille: int

    def __post_init__(self) -> None:
        _bounded_int(self.offset_frames, "offset_frames", 0, MAX_EXTENT_FRAMES_CEILING)
        _bounded_int(self.strength_per_mille, "strength_per_mille", 0, MAX_STRENGTH_PER_MILLE)

    def to_wire(self) -> dict[str, int]:
        return {
            "offset_frames": self.offset_frames,
            "strength_per_mille": self.strength_per_mille,
        }


@dataclass(frozen=True, slots=True)
class TimelineClip:
    """One placed clip; geometry is exact integer frames and never inferred."""

    clip_id: str
    asset_id: str
    kind: MediaKind
    lane: int
    start_frame: int
    frames: int
    source_start_frame: int
    envelope: tuple[EnvelopePoint, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.clip_id, "clip_id")
        _identifier(self.asset_id, "asset_id")
        if self.kind not in _CLIP_KINDS:
            raise _reject("kind_unsupported", "a clip must be video or audio")
        _bounded_int(self.lane, "lane", 0, MAX_LANES_CEILING - 1)
        _bounded_int(self.start_frame, "start_frame", 0, MAX_EXTENT_FRAMES_CEILING)
        _bounded_int(self.frames, "frames", 1, MAX_EXTENT_FRAMES_CEILING)
        _bounded_int(self.source_start_frame, "source_start_frame", 0, MAX_EXTENT_FRAMES_CEILING)
        previous = -1
        for point in self.envelope:
            if not isinstance(point, EnvelopePoint):
                raise _reject("invalid_envelope", "envelope must hold EnvelopePoint values")
            if point.offset_frames <= previous:
                raise _reject("envelope_order", "envelope offsets must strictly increase")
            if point.offset_frames > self.frames:
                raise _reject("envelope_bounds", "an envelope offset exceeds the clip length")
            previous = point.offset_frames

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.frames

    def to_wire(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "lane": self.lane,
            "start_frame": self.start_frame,
            "frames": self.frames,
            "source_start_frame": self.source_start_frame,
            "envelope": [point.to_wire() for point in self.envelope],
        }


@dataclass(frozen=True, slots=True)
class AVLink:
    """One sparse ID-based audiovisual pairing; co-timing is a validated invariant."""

    video_clip_id: str
    audio_clip_id: str

    def __post_init__(self) -> None:
        _identifier(self.video_clip_id, "video_clip_id")
        _identifier(self.audio_clip_id, "audio_clip_id")

    def to_wire(self) -> dict[str, str]:
        return {"video_clip_id": self.video_clip_id, "audio_clip_id": self.audio_clip_id}


@dataclass(frozen=True, slots=True)
class AddClip:
    expected_revision: int
    clip_id: str
    asset_id: str
    lane: int
    start_frame: int
    frames: int
    source_start_frame: int
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RemoveClip:
    expected_revision: int
    clip_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreClip:
    expected_revision: int
    clip: TimelineClip
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class MoveClip:
    expected_revision: int
    clip_id: str
    delta_frames: int
    delta_lanes: int = 0
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class MoveGroup:
    expected_revision: int
    clip_ids: tuple[str, ...]
    delta_frames: int
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class TrimClip:
    expected_revision: int
    clip_id: str
    edge: TrimEdge
    delta_frames: int
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SplitClip:
    expected_revision: int
    clip_id: str
    at_offset_frames: int
    new_clip_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class MergeClips:
    expected_revision: int
    first_clip_id: str
    second_clip_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class LinkClips:
    expected_revision: int
    video_clip_id: str
    audio_clip_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class UnlinkClips:
    expected_revision: int
    video_clip_id: str
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SetEnvelope:
    expected_revision: int
    clip_id: str
    points: tuple[EnvelopePoint, ...]
    expected_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SelectClips:
    expected_revision: int
    clip_ids: tuple[str, ...]
    expected_fingerprint: str | None = None


TimelineCommand = (
    AddClip
    | RemoveClip
    | RestoreClip
    | MoveClip
    | MoveGroup
    | TrimClip
    | SplitClip
    | MergeClips
    | LinkClips
    | UnlinkClips
    | SetEnvelope
    | SelectClips
)

_COMMAND_KINDS: dict[type, CommandKind] = {
    AddClip: CommandKind.ADD_CLIP,
    RemoveClip: CommandKind.REMOVE_CLIP,
    RestoreClip: CommandKind.RESTORE_CLIP,
    MoveClip: CommandKind.MOVE_CLIP,
    MoveGroup: CommandKind.MOVE_GROUP,
    TrimClip: CommandKind.TRIM_CLIP,
    SplitClip: CommandKind.SPLIT_CLIP,
    MergeClips: CommandKind.MERGE_CLIPS,
    LinkClips: CommandKind.LINK_CLIPS,
    UnlinkClips: CommandKind.UNLINK_CLIPS,
    SetEnvelope: CommandKind.SET_ENVELOPE,
    SelectClips: CommandKind.SELECT_CLIPS,
}


@dataclass(frozen=True, slots=True)
class TimelineState:
    """Backend-revisioned authoring state; every mutation is a validated command."""

    profile: TimelineProfileInput
    limits: TimelineAuthoringLimits
    references_revision: int
    references_fingerprint: str
    revision: int = 1
    clips: tuple[TimelineClip, ...] = ()
    links: tuple[AVLink, ...] = ()
    selection: tuple[str, ...] = ()

    def clip(self, clip_id: str) -> TimelineClip | None:
        for candidate in self.clips:
            if candidate.clip_id == clip_id:
                return candidate
        return None

    def link_for(self, clip_id: str) -> AVLink | None:
        for link in self.links:
            if clip_id in (link.video_clip_id, link.audio_clip_id):
                return link
        return None

    def content_fingerprint(self) -> str:
        """The state's material identity: what would feed generation.

        It excludes the revision counter and the selection.  Undo is revision-scoped, so
        replaying a receipt's inverse restores this identity exactly while the revision keeps
        advancing; and selection is authoring ephemera -- removing or merging away a selected
        clip prunes it from the selection, and the inverse restores the material clip without
        pretending the selection never changed.  Selection has its own command and its own
        inverse.
        """

        wire = self.to_wire()
        del wire["revision"]
        del wire["selection"]
        return canonical_fingerprint(wire)

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": TIMELINE_AUTHORING_SCHEMA,
            "revision": self.revision,
            "profile": self.profile.to_wire(),
            "limits": self.limits.to_wire(),
            "references_revision": self.references_revision,
            "references_fingerprint": self.references_fingerprint,
            "clips": [clip.to_wire() for clip in self.clips],
            "links": [link.to_wire() for link in self.links],
            "selection": list(self.selection),
        }


def create_timeline(
    profile: TimelineProfileInput,
    limits: TimelineAuthoringLimits,
    references: TimelineReferenceView,
) -> TimelineState:
    if not isinstance(profile, TimelineProfileInput):
        raise _reject("invalid_profile", "profile must be a TimelineProfileInput")
    if not isinstance(limits, TimelineAuthoringLimits):
        raise _reject("invalid_limits", "limits must be a TimelineAuthoringLimits")
    if not isinstance(references, TimelineReferenceView):
        raise _reject("invalid_reference_view", "references must be a TimelineReferenceView")
    if profile.max_extent_frames < profile.frame_grid:
        raise _reject("profile_extent", "the extent cannot be smaller than the profile grid")
    return TimelineState(
        profile=profile,
        limits=limits,
        references_revision=references.revision,
        references_fingerprint=references.fingerprint,
    )


@dataclass(frozen=True, slots=True)
class TimelineReceipt:
    """One accepted command: what ran, what it produced, and the exact way back."""

    kind: CommandKind
    revision_before: int
    revision_after: int
    subject_ids: tuple[str, ...]
    inverse: TimelineCommand
    state_fingerprint: str


@dataclass(frozen=True, slots=True)
class SnapCandidate:
    frame: int
    kind: SnapKind
    distance: int

    def to_wire(self) -> dict[str, object]:
        return {"frame": self.frame, "kind": self.kind.value, "distance": self.distance}


@dataclass(frozen=True, slots=True)
class TimelineBlocker:
    """One reason the current timeline cannot feed generation; never silently dropped."""

    clip_id: str
    code: str

    def to_wire(self) -> dict[str, str]:
        return {"clip_id": self.clip_id, "code": self.code}


def _max_source_frames(asset: ReferencedAsset, fps: int) -> int:
    if asset.duration_milliseconds is None:
        raise _reject("missing_duration", "a timed clip needs the asset duration")
    return (asset.duration_milliseconds * fps) // 1_000


def _check_source_range(
    asset: ReferencedAsset, source_start_frame: int, frames: int, fps: int
) -> None:
    available = _max_source_frames(asset, fps)
    if source_start_frame + frames > available:
        raise _reject("source_bounds", "the source range exceeds the admitted asset extent")


def _check_geometry(
    state: TimelineState, clip: TimelineClip, *, ignore_ids: frozenset[str] = frozenset()
) -> None:
    if clip.lane >= state.limits.max_lanes:
        raise _reject("lane_bounds", "the lane index exceeds the declared lane ceiling")
    if clip.end_frame > state.profile.max_extent_frames:
        raise _reject("extent_bounds", "the clip ends past the declared timeline extent")
    for other in state.clips:
        if other.clip_id == clip.clip_id or other.clip_id in ignore_ids:
            continue
        if other.kind is not clip.kind or other.lane != clip.lane:
            continue
        if clip.start_frame < other.end_frame and other.start_frame < clip.end_frame:
            raise _reject("overlap", "two clips on one lane cannot overlap")


def _check_asset(
    references: TimelineReferenceView, asset_id: str, kind: MediaKind
) -> ReferencedAsset:
    asset = references.asset(asset_id)
    if asset is None:
        raise _reject("unknown_asset", "the referenced asset is not admitted")
    if asset.kind is not kind:
        raise _reject("kind_mismatch", "the clip kind must match the admitted asset kind")
    return asset


def _sorted_clips(clips: tuple[TimelineClip, ...]) -> tuple[TimelineClip, ...]:
    return tuple(
        sorted(clips, key=lambda clip: (clip.kind.value, clip.lane, clip.start_frame, clip.clip_id))
    )


def _linked_ids(state: TimelineState) -> frozenset[str]:
    ids: set[str] = set()
    for link in state.links:
        ids.add(link.video_clip_id)
        ids.add(link.audio_clip_id)
    return frozenset(ids)


def _require_clip(state: TimelineState, clip_id: str) -> TimelineClip:
    clip = state.clip(clip_id)
    if clip is None:
        raise _reject("unknown_clip", "the command names a clip that does not exist")
    return clip


def _require_unlinked(state: TimelineState, clip_id: str) -> None:
    if state.link_for(clip_id) is not None:
        raise _reject("linked_requires_unlink", "unlink the audiovisual pair before this edit")


def _aligned(state: TimelineState, value: int) -> bool:
    return value % state.profile.audio_period_frames == 0


def _finish(
    state: TimelineState,
    command: TimelineCommand,
    next_state: TimelineState,
    subject_ids: tuple[str, ...],
    inverse: TimelineCommand,
) -> tuple[TimelineState, TimelineReceipt]:
    if state.revision >= MAX_REVISION:
        raise _reject("revision_ceiling", "the revision counter is exhausted")
    produced = replace(next_state, clips=_sorted_clips(next_state.clips))
    receipt = TimelineReceipt(
        kind=_COMMAND_KINDS[type(command)],
        revision_before=state.revision,
        revision_after=produced.revision,
        subject_ids=subject_ids,
        inverse=inverse,
        state_fingerprint=canonical_fingerprint(produced.to_wire()),
    )
    return produced, receipt


def _apply_add(
    state: TimelineState, command: AddClip, references: TimelineReferenceView
) -> tuple[TimelineState, TimelineReceipt]:
    if state.clip(command.clip_id) is not None:
        raise _reject("duplicate_clip", "the clip id already exists")
    if len(state.clips) >= state.limits.max_clips:
        raise _reject("capacity_clips", "the clip ceiling is reached")
    asset = references.asset(command.asset_id)
    if asset is None:
        raise _reject("unknown_asset", "the referenced asset is not admitted")
    if asset.kind not in _CLIP_KINDS:
        raise _reject("kind_unsupported", "only video and audio assets can become clips")
    clip = TimelineClip(
        clip_id=command.clip_id,
        asset_id=command.asset_id,
        kind=asset.kind,
        lane=command.lane,
        start_frame=command.start_frame,
        frames=command.frames,
        source_start_frame=command.source_start_frame,
    )
    _check_source_range(asset, clip.source_start_frame, clip.frames, state.profile.video_fps)
    _check_geometry(state, clip)
    next_state = replace(state, revision=state.revision + 1, clips=state.clips + (clip,))
    inverse = RemoveClip(expected_revision=next_state.revision, clip_id=clip.clip_id)
    return _finish(state, command, next_state, (clip.clip_id,), inverse)


def _apply_remove(
    state: TimelineState, command: RemoveClip
) -> tuple[TimelineState, TimelineReceipt]:
    clip = _require_clip(state, command.clip_id)
    _require_unlinked(state, command.clip_id)
    remaining = tuple(item for item in state.clips if item.clip_id != clip.clip_id)
    selection = tuple(item for item in state.selection if item != clip.clip_id)
    next_state = replace(state, revision=state.revision + 1, clips=remaining, selection=selection)
    inverse = RestoreClip(expected_revision=next_state.revision, clip=clip)
    return _finish(state, command, next_state, (clip.clip_id,), inverse)


def _apply_restore(
    state: TimelineState, command: RestoreClip, references: TimelineReferenceView
) -> tuple[TimelineState, TimelineReceipt]:
    clip = command.clip
    if not isinstance(clip, TimelineClip):
        raise _reject("invalid_clip", "restore must carry a TimelineClip")
    if state.clip(clip.clip_id) is not None:
        raise _reject("duplicate_clip", "the clip id already exists")
    if len(state.clips) >= state.limits.max_clips:
        raise _reject("capacity_clips", "the clip ceiling is reached")
    asset = _check_asset(references, clip.asset_id, clip.kind)
    _check_source_range(asset, clip.source_start_frame, clip.frames, state.profile.video_fps)
    _check_geometry(state, clip)
    next_state = replace(state, revision=state.revision + 1, clips=state.clips + (clip,))
    inverse = RemoveClip(expected_revision=next_state.revision, clip_id=clip.clip_id)
    return _finish(state, command, next_state, (clip.clip_id,), inverse)


def _moved_clip(clip: TimelineClip, delta_frames: int, delta_lanes: int) -> TimelineClip:
    new_start = clip.start_frame + delta_frames
    new_lane = clip.lane + delta_lanes
    if new_start < 0:
        raise _reject("extent_bounds", "a clip cannot start before frame zero")
    if new_lane < 0:
        raise _reject("lane_bounds", "a clip cannot move below lane zero")
    return replace(clip, start_frame=new_start, lane=new_lane)


def _apply_move(state: TimelineState, command: MoveClip) -> tuple[TimelineState, TimelineReceipt]:
    clip = _require_clip(state, command.clip_id)
    _bounded_int(
        command.delta_frames, "delta_frames", -MAX_EXTENT_FRAMES_CEILING, MAX_EXTENT_FRAMES_CEILING
    )
    _bounded_int(command.delta_lanes, "delta_lanes", -MAX_LANES_CEILING, MAX_LANES_CEILING)
    link = state.link_for(command.clip_id)
    moved: dict[str, TimelineClip] = {}
    if link is not None:
        if command.delta_lanes != 0:
            raise _reject("linked_requires_unlink", "a linked clip cannot change lanes")
        if not _aligned(state, command.delta_frames):
            raise _reject("audio_alignment", "a linked pair moves only in exact audio-period steps")
        for member_id in (link.video_clip_id, link.audio_clip_id):
            member = _require_clip(state, member_id)
            moved[member_id] = _moved_clip(member, command.delta_frames, 0)
    else:
        moved[clip.clip_id] = _moved_clip(clip, command.delta_frames, command.delta_lanes)
    next_clips = tuple(moved.get(item.clip_id, item) for item in state.clips)
    probe = replace(state, clips=next_clips)
    for item in moved.values():
        _check_geometry(probe, item)
    next_state = replace(state, revision=state.revision + 1, clips=next_clips)
    inverse = MoveClip(
        expected_revision=next_state.revision,
        clip_id=command.clip_id,
        delta_frames=-command.delta_frames,
        delta_lanes=-command.delta_lanes,
    )
    return _finish(state, command, next_state, tuple(sorted(moved)), inverse)


def _apply_move_group(
    state: TimelineState, command: MoveGroup
) -> tuple[TimelineState, TimelineReceipt]:
    ids = command.clip_ids
    if not ids or len(ids) > state.limits.max_group:
        raise _reject("group_bounds", "the group size is out of bounds")
    if len(set(ids)) != len(ids):
        raise _reject("group_duplicate", "the group repeats a clip id")
    _bounded_int(
        command.delta_frames, "delta_frames", -MAX_EXTENT_FRAMES_CEILING, MAX_EXTENT_FRAMES_CEILING
    )
    members = {clip_id: _require_clip(state, clip_id) for clip_id in ids}
    group = frozenset(ids)
    linked = False
    for link in state.links:
        joined = {link.video_clip_id, link.audio_clip_id}
        if joined & group:
            linked = True
            if not joined <= group:
                raise _reject(
                    "link_split_by_group", "a linked pair must move as one; include both clips"
                )
    if linked and not _aligned(state, command.delta_frames):
        raise _reject("audio_alignment", "a linked pair moves only in exact audio-period steps")
    moved = {
        clip_id: _moved_clip(clip, command.delta_frames, 0) for clip_id, clip in members.items()
    }
    next_clips = tuple(moved.get(item.clip_id, item) for item in state.clips)
    probe = replace(state, clips=next_clips)
    for item in moved.values():
        _check_geometry(probe, item, ignore_ids=group - {item.clip_id})
        for other_id in group - {item.clip_id}:
            other = moved[other_id]
            if other.kind is item.kind and other.lane == item.lane:
                if item.start_frame < other.end_frame and other.start_frame < item.end_frame:
                    raise _reject("overlap", "two clips on one lane cannot overlap")
    next_state = replace(state, revision=state.revision + 1, clips=next_clips)
    inverse = MoveGroup(
        expected_revision=next_state.revision,
        clip_ids=command.clip_ids,
        delta_frames=-command.delta_frames,
    )
    return _finish(state, command, next_state, tuple(sorted(ids)), inverse)


def _apply_trim(
    state: TimelineState, command: TrimClip, references: TimelineReferenceView
) -> tuple[TimelineState, TimelineReceipt]:
    clip = _require_clip(state, command.clip_id)
    _require_unlinked(state, command.clip_id)
    if not isinstance(command.edge, TrimEdge):
        raise _reject("invalid_edge", "edge must be a TrimEdge")
    delta = _bounded_int(
        command.delta_frames,
        "delta_frames",
        -MAX_EXTENT_FRAMES_CEILING,
        MAX_EXTENT_FRAMES_CEILING,
    )
    if delta == 0:
        raise _reject("trim_bounds", "a trim must change the boundary")
    if command.edge is TrimEdge.START:
        new_start = clip.start_frame + delta
        new_source = clip.source_start_frame + delta
        new_frames = clip.frames - delta
        if new_start < 0 or new_source < 0 or new_frames < 1:
            raise _reject("trim_bounds", "the start trim leaves no valid clip")
        shifted: list[EnvelopePoint] = []
        for point in clip.envelope:
            offset = point.offset_frames - delta
            if offset < 0 or offset > new_frames:
                raise _reject(
                    "envelope_bounds", "the trim would drop an envelope point; edit it first"
                )
            shifted.append(replace(point, offset_frames=offset))
        trimmed = replace(
            clip,
            start_frame=new_start,
            source_start_frame=new_source,
            frames=new_frames,
            envelope=tuple(shifted),
        )
    else:
        new_frames = clip.frames + delta
        if new_frames < 1:
            raise _reject("trim_bounds", "the end trim leaves no valid clip")
        for point in clip.envelope:
            if point.offset_frames > new_frames:
                raise _reject(
                    "envelope_bounds", "the trim would drop an envelope point; edit it first"
                )
        trimmed = replace(clip, frames=new_frames)
    asset = _check_asset(references, clip.asset_id, clip.kind)
    _check_source_range(asset, trimmed.source_start_frame, trimmed.frames, state.profile.video_fps)
    probe = replace(
        state, clips=tuple(item for item in state.clips if item.clip_id != clip.clip_id)
    )
    _check_geometry(probe, trimmed)
    next_clips = tuple(trimmed if item.clip_id == clip.clip_id else item for item in state.clips)
    next_state = replace(state, revision=state.revision + 1, clips=next_clips)
    inverse = TrimClip(
        expected_revision=next_state.revision,
        clip_id=command.clip_id,
        edge=command.edge,
        delta_frames=-delta,
    )
    return _finish(state, command, next_state, (clip.clip_id,), inverse)


def _apply_split(state: TimelineState, command: SplitClip) -> tuple[TimelineState, TimelineReceipt]:
    clip = _require_clip(state, command.clip_id)
    _require_unlinked(state, command.clip_id)
    _identifier(command.new_clip_id, "new_clip_id")
    if state.clip(command.new_clip_id) is not None:
        raise _reject("duplicate_clip", "the new clip id already exists")
    if len(state.clips) >= state.limits.max_clips:
        raise _reject("capacity_clips", "the clip ceiling is reached")
    at = _bounded_int(command.at_offset_frames, "at_offset_frames", 1, MAX_EXTENT_FRAMES_CEILING)
    if at >= clip.frames:
        raise _reject("split_bounds", "the split offset must fall strictly inside the clip")
    first_points = tuple(point for point in clip.envelope if point.offset_frames < at)
    second_points = tuple(
        replace(point, offset_frames=point.offset_frames - at)
        for point in clip.envelope
        if point.offset_frames >= at
    )
    first = replace(clip, frames=at, envelope=first_points)
    second = replace(
        clip,
        clip_id=command.new_clip_id,
        start_frame=clip.start_frame + at,
        frames=clip.frames - at,
        source_start_frame=clip.source_start_frame + at,
        envelope=second_points,
    )
    next_clips = tuple(first if item.clip_id == clip.clip_id else item for item in state.clips) + (
        second,
    )
    next_state = replace(state, revision=state.revision + 1, clips=next_clips)
    inverse = MergeClips(
        expected_revision=next_state.revision,
        first_clip_id=clip.clip_id,
        second_clip_id=command.new_clip_id,
    )
    return _finish(state, command, next_state, (clip.clip_id, command.new_clip_id), inverse)


def _apply_merge(
    state: TimelineState, command: MergeClips
) -> tuple[TimelineState, TimelineReceipt]:
    first = _require_clip(state, command.first_clip_id)
    second = _require_clip(state, command.second_clip_id)
    _require_unlinked(state, command.first_clip_id)
    _require_unlinked(state, command.second_clip_id)
    if (
        first.asset_id != second.asset_id
        or first.kind is not second.kind
        or first.lane != second.lane
        or first.end_frame != second.start_frame
        or first.source_start_frame + first.frames != second.source_start_frame
    ):
        raise _reject(
            "merge_incompatible",
            "merge requires one asset, one lane, and exact timeline and source contiguity",
        )
    merged_points = first.envelope + tuple(
        replace(point, offset_frames=point.offset_frames + first.frames)
        for point in second.envelope
    )
    merged = replace(first, frames=first.frames + second.frames, envelope=merged_points)
    selection = tuple(item for item in state.selection if item != second.clip_id)
    next_clips = tuple(
        merged if item.clip_id == first.clip_id else item
        for item in state.clips
        if item.clip_id != second.clip_id
    )
    next_state = replace(state, revision=state.revision + 1, clips=next_clips, selection=selection)
    inverse = SplitClip(
        expected_revision=next_state.revision,
        clip_id=first.clip_id,
        at_offset_frames=first.frames,
        new_clip_id=second.clip_id,
    )
    return _finish(
        state, command, next_state, (command.first_clip_id, command.second_clip_id), inverse
    )


def _apply_link(state: TimelineState, command: LinkClips) -> tuple[TimelineState, TimelineReceipt]:
    video = _require_clip(state, command.video_clip_id)
    audio = _require_clip(state, command.audio_clip_id)
    if video.kind is not MediaKind.VIDEO or audio.kind is not MediaKind.AUDIO:
        raise _reject("link_kind", "a link joins exactly one video clip and one audio clip")
    if state.link_for(video.clip_id) is not None or state.link_for(audio.clip_id) is not None:
        raise _reject("link_exists", "a clip can belong to at most one link")
    if video.start_frame != audio.start_frame or video.frames != audio.frames:
        raise _reject("link_alignment", "a linked pair must share start and length exactly")
    if not _aligned(state, video.start_frame) or not _aligned(state, video.frames):
        raise _reject("audio_alignment", "a linked pair must sit on exact audio-period boundaries")
    link = AVLink(video_clip_id=video.clip_id, audio_clip_id=audio.clip_id)
    next_state = replace(state, revision=state.revision + 1, links=state.links + (link,))
    inverse = UnlinkClips(expected_revision=next_state.revision, video_clip_id=video.clip_id)
    return _finish(state, command, next_state, (video.clip_id, audio.clip_id), inverse)


def _apply_unlink(
    state: TimelineState, command: UnlinkClips
) -> tuple[TimelineState, TimelineReceipt]:
    link = None
    for candidate in state.links:
        if candidate.video_clip_id == command.video_clip_id:
            link = candidate
    if link is None:
        raise _reject("unknown_link", "no link exists for the named video clip")
    remaining = tuple(item for item in state.links if item is not link)
    next_state = replace(state, revision=state.revision + 1, links=remaining)
    inverse = LinkClips(
        expected_revision=next_state.revision,
        video_clip_id=link.video_clip_id,
        audio_clip_id=link.audio_clip_id,
    )
    return _finish(state, command, next_state, (link.video_clip_id, link.audio_clip_id), inverse)


def _apply_set_envelope(
    state: TimelineState, command: SetEnvelope
) -> tuple[TimelineState, TimelineReceipt]:
    clip = _require_clip(state, command.clip_id)
    if len(command.points) > state.limits.max_envelope_points:
        raise _reject("envelope_points", "the envelope point ceiling is exceeded")
    updated = replace(clip, envelope=tuple(command.points))
    next_clips = tuple(updated if item.clip_id == clip.clip_id else item for item in state.clips)
    next_state = replace(state, revision=state.revision + 1, clips=next_clips)
    inverse = SetEnvelope(
        expected_revision=next_state.revision, clip_id=clip.clip_id, points=clip.envelope
    )
    return _finish(state, command, next_state, (clip.clip_id,), inverse)


def _apply_select(
    state: TimelineState, command: SelectClips
) -> tuple[TimelineState, TimelineReceipt]:
    if len(command.clip_ids) > state.limits.max_selection:
        raise _reject("selection_bounds", "the selection ceiling is exceeded")
    if len(set(command.clip_ids)) != len(command.clip_ids):
        raise _reject("selection_bounds", "the selection repeats a clip id")
    for clip_id in command.clip_ids:
        _require_clip(state, clip_id)
    next_state = replace(state, revision=state.revision + 1, selection=tuple(command.clip_ids))
    inverse = SelectClips(expected_revision=next_state.revision, clip_ids=state.selection)
    return _finish(state, command, next_state, tuple(command.clip_ids), inverse)


def apply_command(
    state: TimelineState,
    command: TimelineCommand,
    references: TimelineReferenceView,
) -> tuple[TimelineState, TimelineReceipt]:
    """Apply one command atomically: one exact next state, or a typed rejection."""

    if not isinstance(state, TimelineState):
        raise _reject("invalid_state", "state must be a TimelineState")
    if type(command) not in _COMMAND_KINDS:
        raise _reject("unknown_command", "the command type is not part of this contract")
    if not isinstance(references, TimelineReferenceView):
        raise _reject("invalid_reference_view", "references must be a TimelineReferenceView")
    _bounded_int(command.expected_revision, "expected_revision", 1, MAX_REVISION)
    if command.expected_revision != state.revision:
        raise _reject("stale_revision", "the command names a revision that is not current")
    if command.expected_fingerprint is not None:
        _fingerprint(command.expected_fingerprint, "expected_fingerprint")
        if command.expected_fingerprint != canonical_fingerprint(state.to_wire()):
            raise _reject("stale_fingerprint", "the command names a state that is not current")
    if references.revision < state.references_revision:
        raise _reject("stale_references", "the reference view is older than the state's view")
    adopted = replace(
        state,
        references_revision=references.revision,
        references_fingerprint=references.fingerprint,
    )
    if isinstance(command, AddClip):
        return _apply_add(adopted, command, references)
    if isinstance(command, RemoveClip):
        return _apply_remove(adopted, command)
    if isinstance(command, RestoreClip):
        return _apply_restore(adopted, command, references)
    if isinstance(command, MoveClip):
        return _apply_move(adopted, command)
    if isinstance(command, MoveGroup):
        return _apply_move_group(adopted, command)
    if isinstance(command, TrimClip):
        return _apply_trim(adopted, command, references)
    if isinstance(command, SplitClip):
        return _apply_split(adopted, command)
    if isinstance(command, MergeClips):
        return _apply_merge(adopted, command)
    if isinstance(command, LinkClips):
        return _apply_link(adopted, command)
    if isinstance(command, UnlinkClips):
        return _apply_unlink(adopted, command)
    if isinstance(command, SetEnvelope):
        return _apply_set_envelope(adopted, command)
    return _apply_select(adopted, command)


def snap_candidates(
    state: TimelineState,
    *,
    frame: int,
    playhead_frame: int | None = None,
    exclude_clip_id: str | None = None,
    max_candidates: int = 8,
) -> tuple[SnapCandidate, ...]:
    """Bounded advisory snap candidates; deterministic and never a constraint override.

    Ordering is exact: ascending distance, then boundary before grid before playhead, then the
    lower frame.  The caller remains free to ignore every candidate; hard alignment rules stay
    in the commands.
    """

    if not isinstance(state, TimelineState):
        raise _reject("invalid_state", "state must be a TimelineState")
    _bounded_int(frame, "frame", 0, state.profile.max_extent_frames)
    _bounded_int(max_candidates, "max_candidates", 1, MAX_SNAP_CANDIDATES)
    if playhead_frame is not None:
        _bounded_int(playhead_frame, "playhead_frame", 0, state.profile.max_extent_frames)
    best: dict[int, SnapCandidate] = {}

    def offer(candidate_frame: int, kind: SnapKind) -> None:
        if candidate_frame < 0 or candidate_frame > state.profile.max_extent_frames:
            return
        candidate = SnapCandidate(
            frame=candidate_frame, kind=kind, distance=abs(candidate_frame - frame)
        )
        current = best.get(candidate_frame)
        if current is None or _SNAP_PRIORITY[kind] < _SNAP_PRIORITY[current.kind]:
            best[candidate_frame] = candidate

    for clip in state.clips:
        if exclude_clip_id is not None and clip.clip_id == exclude_clip_id:
            continue
        offer(clip.start_frame, SnapKind.CLIP_BOUNDARY)
        offer(clip.end_frame, SnapKind.CLIP_BOUNDARY)
    grid = state.profile.frame_grid
    offer((frame // grid) * grid, SnapKind.GRID)
    offer(((frame // grid) + 1) * grid, SnapKind.GRID)
    if playhead_frame is not None:
        offer(playhead_frame, SnapKind.PLAYHEAD)
    ordered = sorted(
        best.values(),
        key=lambda item: (item.distance, _SNAP_PRIORITY[item.kind], item.frame),
    )
    return tuple(ordered[:max_candidates])


def timeline_blockers(
    state: TimelineState, references: TimelineReferenceView
) -> tuple[TimelineBlocker, ...]:
    """Every reason the timeline cannot feed generation right now, in canonical clip order.

    Reference drift is reported, never repaired: a clip whose admitted asset disappeared or
    changed kind blocks the queue until the user acts through commands.
    """

    if not isinstance(state, TimelineState):
        raise _reject("invalid_state", "state must be a TimelineState")
    if not isinstance(references, TimelineReferenceView):
        raise _reject("invalid_reference_view", "references must be a TimelineReferenceView")
    blockers: list[TimelineBlocker] = []
    if references.revision < state.references_revision:
        blockers.append(TimelineBlocker(clip_id="", code="stale_references"))
        return tuple(blockers)
    for clip in state.clips:
        asset = references.asset(clip.asset_id)
        if asset is None:
            blockers.append(TimelineBlocker(clip_id=clip.clip_id, code="asset_missing"))
            continue
        if asset.kind is not clip.kind:
            blockers.append(TimelineBlocker(clip_id=clip.clip_id, code="kind_drift"))
            continue
        if asset.duration_milliseconds is None:
            blockers.append(TimelineBlocker(clip_id=clip.clip_id, code="missing_duration"))
            continue
        available = (asset.duration_milliseconds * state.profile.video_fps) // 1_000
        if clip.source_start_frame + clip.frames > available:
            blockers.append(TimelineBlocker(clip_id=clip.clip_id, code="source_overrun"))
    return tuple(blockers)
