"""Closed portable editable data, deliberately separate from runtime authority."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, cast

from .composition_contract import (
    INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
    AudioExtension,
    CompositionTrack,
    _asset,
    _audio_extension,
    _clip,
    _track,
)
from .errors import ContractValidationError
from .length import resolve_milliseconds
from .nle_authoring_contract import NleAuthoringState, create_nle_authoring_state
from .segment_workspace import MAX_WORKSPACE_SEGMENTS, SegmentRelationKind

MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_PROJECT_DEPTH = 32
MAX_PROJECT_JSON_NODES = 65_536
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FP = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RETAINED = re.compile(r"asset_[0-9a-f]{32}\Z")
_MODES = {"t2va", "i2va", "l2va", "fl2va", "ref2va"}
_POLICIES = {"auto_storyboard", "fixed_5", "fixed_10", "fixed_12", "fixed_15"}


class ProjectDocumentError(ValueError):
    """Finite, content-free failure; never include user document data."""

    def __init__(self, code: str = "document_invalid") -> None:
        self.code = code
        super().__init__(code)


def closed(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ProjectDocumentError()
    return cast(dict[str, Any], value)


def integer(value: object, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ProjectDocumentError()
    return value


def identifier(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ProjectDocumentError()
    return value


def text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ProjectDocumentError()
    return value


def rows(value: object, maximum: int) -> list[Any]:
    if type(value) is not list or len(value) > maximum:
        raise ProjectDocumentError()
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ProjectDocumentError("duplicate_key")
        result[key] = value
    return result


def project_json(data: bytes, *, maximum_bytes: int = MAX_PROJECT_BYTES) -> object:
    if type(data) is not bytes or not 1 <= len(data) <= maximum_bytes:
        raise ProjectDocumentError("document_size")
    try:
        value: object = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ProjectDocumentError()),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ProjectDocumentError() from None
    pending, count = [(value, 0)], 0
    while pending:
        child, depth = pending.pop()
        count += 1
        if depth > MAX_PROJECT_DEPTH or count > MAX_PROJECT_JSON_NODES:
            raise ProjectDocumentError("document_size")
        if type(child) is dict:
            pending.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            pending.extend((item, depth + 1) for item in child)
    return value


def project_bytes(value: object, *, maximum_bytes: int = MAX_PROJECT_BYTES) -> bytes:
    try:
        data = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise ProjectDocumentError() from None
    if len(data) > maximum_bytes:
        raise ProjectDocumentError("document_size")
    return data


@dataclass(frozen=True, slots=True)
class ProjectSegment:
    segment_id: str
    task_mode: str
    duration_milliseconds: int
    relation: str
    predecessor_segment_id: str | None
    source_asset_id: str | None
    reference_asset_ids: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "task_mode": self.task_mode,
            "duration_milliseconds": self.duration_milliseconds,
            "relation": self.relation,
            "predecessor_segment_id": self.predecessor_segment_id,
            "source_asset_id": self.source_asset_id,
            "reference_asset_ids": list(self.reference_asset_ids),
        }


@dataclass(frozen=True, slots=True)
class ProjectMediaReference:
    asset_id: str
    content_fingerprint: str | None
    byte_length: int | None
    retained_id: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "content_fingerprint": self.content_fingerprint,
            "byte_length": self.byte_length,
            "retained_id": self.retained_id,
        }


@dataclass(frozen=True, slots=True)
class ProjectDocument:
    title: str
    segments: tuple[ProjectSegment, ...]
    selection: tuple[str, ...]
    planning_json: str
    editor: NleAuthoringState | None
    reference_json: str
    media: tuple[ProjectMediaReference, ...]

    @property
    def planning(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.planning_json))

    @property
    def reference(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.reference_json))

    def to_wire(self) -> dict[str, object]:
        editor: dict[str, object] | None = None
        if self.editor is not None:
            editor = editable_authoring_wire(self.editor)
            editor["reference"] = self.reference
        return {
            "format": "h3proj",
            "schema_version": 1,
            "title": self.title,
            "production": {
                "segments": [row.to_wire() for row in self.segments],
                "selection": list(self.selection),
            },
            "planning": self.planning,
            "editor": editor,
            "media": [row.to_wire() for row in self.media],
        }

    def fresh_authoring(self, project_id: str, workspace_handle: str) -> NleAuthoringState:
        state = self.editor
        return create_nle_authoring_state(
            project_id=project_id,
            workspace_handle=workspace_handle,
            workspace_revision=1,
            timeline_revision=1,
            edit_capacity_frames=3600 if state is None else state.edit_capacity_frames,
            assets=() if state is None else state.assets,
            tracks=(CompositionTrack("track.main", "primary_video", 0, True, False),)
            if state is None
            else state.tracks,
            clips=() if state is None else state.clips,
            audio_extension=default_project_audio() if state is None else state.audio_extension,
        )


def default_project_audio() -> AudioExtension:
    return AudioExtension(
        "h3.authoring.independent_audio_extension.v1",
        "none_v1",
        INDEPENDENT_AUDIO_COMMAND_NAMESPACE,
        (),
        "unsupported",
        "unsupported",
        "EmbeddedAudioSpanV1",
        "none_v1",
        "audio_editing_deferred",
    )


def editable_authoring_wire(state: NleAuthoringState) -> dict[str, object]:
    # SECURITY: explicit fields only; the public state also contains runtime identities and CAS.
    return {
        "edit_capacity_frames": state.edit_capacity_frames,
        "assets": [asset.to_wire() for asset in state.assets],
        "tracks": [track.to_wire() for track in state.tracks],
        "clips": [clip.to_wire() for clip in state.clips],
        "audio_extension": state.audio_extension.to_wire(),
    }


def _planning(value: object) -> dict[str, Any]:
    wire = closed(value, {"intent", "script", "target_seconds", "policy", "shots"})
    text(wire["intent"], 65_536)
    text(wire["script"], 65_536)
    integer(wire["target_seconds"], 4, 60)
    if type(wire["policy"]) is not str or wire["policy"] not in _POLICIES:
        raise ProjectDocumentError()
    seen: set[str] = set()
    for index, value in enumerate(rows(wire["shots"], 32), 1):
        shot = closed(
            value,
            {
                "shot_id",
                "ordinal",
                "start_milliseconds",
                "end_milliseconds",
                "text",
                "hard_boundary",
            },
        )
        shot_id = identifier(shot["shot_id"])
        if shot_id in seen or integer(shot["ordinal"], 1, 32) != index:
            raise ProjectDocumentError()
        seen.add(shot_id)
        start = integer(shot["start_milliseconds"], 0, 60_000)
        end = integer(shot["end_milliseconds"], 1, 60_000)
        if start >= end or type(shot["hard_boundary"]) is not bool:
            raise ProjectDocumentError()
        text(shot["text"], 8192)
    return wire


def _reference(value: object) -> dict[str, Any]:
    wire = closed(value, {"sources", "soundtracks"})
    seen: set[str] = set()
    kinds: dict[str, str] = {}
    for value in rows(wire["sources"], 128):
        row = closed(value, {"source_id", "kind", "duration_milliseconds"})
        key = identifier(row["source_id"])
        if (
            key in seen
            or type(row["kind"]) is not str
            or row["kind"] not in {"image", "video", "audio"}
        ):
            raise ProjectDocumentError()
        seen.add(key)
        kinds[key] = row["kind"]
        if row["kind"] == "image":
            if row["duration_milliseconds"] is not None:
                raise ProjectDocumentError()
        elif row["duration_milliseconds"] is not None:
            integer(row["duration_milliseconds"], 1, 60_000)
    videos: set[str] = set()
    for value in rows(wire["soundtracks"], 128):
        row = closed(value, {"video_id", "intent", "soundtrack_source_id"})
        video = identifier(row["video_id"])
        audio = row["soundtrack_source_id"]
        if (
            video in videos
            or kinds.get(video) != "video"
            or row["intent"] not in {"included", "excluded"}
        ):
            raise ProjectDocumentError()
        videos.add(video)
        if row["intent"] == "included":
            if kinds.get(identifier(audio)) != "audio":
                raise ProjectDocumentError()
        elif audio is not None:
            raise ProjectDocumentError()
    return wire


def decode_project_value(value: object) -> ProjectDocument:
    try:
        wire = closed(
            value,
            {"format", "schema_version", "title", "production", "planning", "editor", "media"},
        )
        if (
            wire["format"] != "h3proj"
            or type(wire["schema_version"]) is not int
            or wire["schema_version"] != 1
        ):
            raise ProjectDocumentError("document_version")
        title = text(wire["title"], 256)
        production = closed(wire["production"], {"segments", "selection"})
        segments: list[ProjectSegment] = []
        seen: set[str] = set()
        source_ids: set[str] = set()
        for value in rows(production["segments"], MAX_WORKSPACE_SEGMENTS):
            row = closed(
                value,
                {
                    "segment_id",
                    "task_mode",
                    "duration_milliseconds",
                    "relation",
                    "predecessor_segment_id",
                    "source_asset_id",
                    "reference_asset_ids",
                },
            )
            key = identifier(row["segment_id"])
            if key in seen or type(row["task_mode"]) is not str or row["task_mode"] not in _MODES:
                raise ProjectDocumentError()
            duration = integer(row["duration_milliseconds"], 4000, 15000)
            resolve_milliseconds(duration)
            if type(row["relation"]) is not str:
                raise ProjectDocumentError()
            relation = SegmentRelationKind(row["relation"])
            predecessor = row["predecessor_segment_id"]
            if relation in {SegmentRelationKind.PREDECESSOR, SegmentRelationKind.ADJACENT_PAIR}:
                if identifier(predecessor) not in seen:
                    raise ProjectDocumentError()
            elif predecessor is not None:
                raise ProjectDocumentError()
            source = None if row["source_asset_id"] is None else identifier(row["source_asset_id"])
            references = tuple(identifier(item) for item in rows(row["reference_asset_ids"], 128))
            if len(set(references)) != len(references):
                raise ProjectDocumentError()
            if source is not None:
                source_ids.add(source)
            source_ids.update(references)
            segments.append(
                ProjectSegment(
                    key, row["task_mode"], duration, relation.value, predecessor, source, references
                )
            )
            seen.add(key)
        selection = tuple(
            identifier(item) for item in rows(production["selection"], MAX_WORKSPACE_SEGMENTS)
        )
        if len(set(selection)) != len(selection) or not set(selection).issubset(seen):
            raise ProjectDocumentError()
        planning = _planning(wire["planning"])
        reference: dict[str, Any] = {"sources": [], "soundtracks": []}
        state = None
        if wire["editor"] is not None:
            editor = closed(
                wire["editor"],
                {
                    "edit_capacity_frames",
                    "assets",
                    "tracks",
                    "clips",
                    "audio_extension",
                    "reference",
                },
            )
            reference = _reference(editor["reference"])
            state = create_nle_authoring_state(
                project_id="project.portable",
                workspace_handle="authoring-portable",
                workspace_revision=1,
                timeline_revision=1,
                edit_capacity_frames=integer(editor["edit_capacity_frames"], 3600, 3600),
                assets=tuple(
                    _asset(item, index) for index, item in enumerate(rows(editor["assets"], 128))
                ),
                tracks=tuple(
                    _track(item, index) for index, item in enumerate(rows(editor["tracks"], 8))
                ),
                clips=tuple(
                    _clip(item, index) for index, item in enumerate(rows(editor["clips"], 128))
                ),
                audio_extension=_audio_extension(editor["audio_extension"]),
            )
            if editable_authoring_wire(state) != {
                k: v for k, v in editor.items() if k != "reference"
            }:
                raise ProjectDocumentError()
            source_ids.update(asset.asset_id for asset in state.assets if asset.kind != "font")
            source_ids.update(row["source_id"] for row in reference["sources"])
        media: list[ProjectMediaReference] = []
        seen_media: set[str] = set()
        for value in rows(wire["media"], 128):
            row = closed(value, {"asset_id", "content_fingerprint", "byte_length", "retained_id"})
            key = identifier(row["asset_id"])
            if key in seen_media:
                raise ProjectDocumentError()
            seen_media.add(key)
            digest, length, retained = (
                row["content_fingerprint"],
                row["byte_length"],
                row["retained_id"],
            )
            if digest is None:
                if length is not None or retained is not None:
                    raise ProjectDocumentError()
            elif type(digest) is not str or _FP.fullmatch(digest) is None:
                raise ProjectDocumentError()
            else:
                integer(length, 1, 64 * 1024 * 1024)
            if retained is not None and (
                type(retained) is not str or _RETAINED.fullmatch(retained) is None
            ):
                raise ProjectDocumentError()
            media.append(ProjectMediaReference(key, digest, length, retained))
        if source_ids != seen_media:
            raise ProjectDocumentError()
        result = ProjectDocument(
            title,
            tuple(segments),
            selection,
            project_bytes(planning).decode(),
            state,
            project_bytes(reference).decode(),
            tuple(media),
        )
        if result.to_wire() != wire:
            raise ProjectDocumentError()
        encode_project_document(result)
        return result
    except (
        ContractValidationError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        RecursionError,
    ) as error:
        if isinstance(error, ProjectDocumentError):
            raise
        raise ProjectDocumentError() from None


def decode_project_document(data: bytes) -> ProjectDocument:
    return decode_project_value(project_json(data))


def encode_project_document(document: ProjectDocument) -> bytes:
    if type(document) is not ProjectDocument:
        raise ProjectDocumentError()
    # Full512 timing arrays are valid; the general canonicalizer's256-item ceiling is not used.
    return project_bytes(document.to_wire())


def project_fingerprint(document: ProjectDocument) -> str:
    return "sha256:" + hashlib.sha256(encode_project_document(document)).hexdigest()
