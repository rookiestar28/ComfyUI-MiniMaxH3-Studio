"""M25-20 corrective (B-65): the imported-generated-source scenario, run by both halves alike.

The two ``import_integration`` corpus rows (original plan Section 13 / AC10) require one exact
causal chain: a generated per-segment output that the real M25-29 import service admits into the
Authoring asset bin, an explicit M25-11 insertion of that minted asset, the real M25-18 render
plan bound to the imported source, the M25-19 verified final receipt, and undo/redo through the
pre-import history that keeps the admitted library. This module is the one place that chain is
set up, so the browser fixture (``scripts/m25_16_import_fixture.py``, behind the real shell) and
the render stage (``scripts/nle_semantic_render.py``) drive the same registries with the same
generated source and mint the same identities, which the join then checks against each other.

What is real here: the production and authoring registries, the import service, the timeline
history and its commands, the media probe (the pinned ``ffprobe`` through the qualified adapter),
the source lease the render plan binds, and the artifact bytes themselves -- a real CFR H.264+AAC
MP4 painted from ``IMPORTED_SOURCE_PROFILE`` by the same builder the corpus sources use. Nothing
is stubbed: the accepted M25-29 test module's ``_registries_with_ready_output`` is reused with
``artifact_bodies`` set to that file, exactly as its own real-encoded-artifact test does.

The inserted clip follows the product's own insert control: the first video asset is placed on
the primary video track at ``content_end_exclusive``; its duration is the measured source interval
at the fixed 24 fps authoring rate, clamped once to the remaining edit capacity; source start is 0.
The identity transform and ``clip-r<revision>-1`` are shared with the product command builder, so
the composition the render stage renders is the composition the browser journey produced through
that control, and the join can demand fingerprint equality between the two.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import test_m25_29_production_authoring_import as accepted  # noqa: E402

from comfyui_h3_context.adapters.av_reconstruction_media import (  # noqa: E402
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (  # noqa: E402
    AUTHORING_ACTION_SCHEMA,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import (  # noqa: E402
    ProductionWorkspaceRegistry,
)
from comfyui_h3_context.adapters.production_authoring_import_service import (  # noqa: E402
    ProductionAuthoringImportService,
)
from comfyui_h3_context.core.nle_authoring_contract import (  # noqa: E402
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
)
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    IMPORT_IDENTITY_FACTS,
)
from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    IMPORTED_SOURCE_FRAME_COUNT,
    IMPORTED_SOURCE_PROFILE,
    SOURCE_HEIGHT,
    SOURCE_WIDTH,
    imported_source_landmarks_match,
)

TIMELINE_TRANSACTION_SCHEMA = "h3.context.timeline_transaction.v1"
IMPORT_DEADLINE_SECONDS = 60.0
AUTHORING_FRAME_RATE_NUM = 24
AUTHORING_FRAME_RATE_DEN = 1
INSERTED_CLIP_SOURCE_START_FRAME = 0


class ImportScenarioError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_imported_source_media(ffmpeg: Path, destination: Path) -> bytes:
    """Paint and encode the imported source with the corpus's own media builder."""

    # Lazy: the render stage module imports torch for the image reference source, which the
    # browser fixture process only pays for in this mode.
    from scripts.nle_semantic_render import _build_source_video

    destination.parent.mkdir(parents=True, exist_ok=True)
    _build_source_video(ffmpeg, destination, profile=IMPORTED_SOURCE_PROFILE)
    return destination.read_bytes()


@dataclass(slots=True)
class ImportScenario:
    root: Path
    production: ProductionWorkspaceRegistry
    projection: Any
    authoring: AuthoringWorkspaceRegistry
    authoring_projection: dict[str, Any]
    history: dict[str, Any]
    service: ProductionAuthoringImportService
    adapter: QualifiedAVMediaAdapter
    source_fingerprint: str
    source_byte_length: int
    _owns_root: bool = False

    @property
    def context_handle(self) -> str:
        return accepted_context_handle()

    def close(self) -> None:
        if self._owns_root:
            shutil.rmtree(self.root, ignore_errors=True)


def accepted_context_handle() -> str:
    return "ws_" + "c" * 32


def build_scenario(
    ffmpeg: Path,
    ffprobe: Path,
    *,
    root: Path | None = None,
    segments: int = 1,
    clock_ms: Callable[[], int] | None = None,
) -> ImportScenario:
    """The real registries with one ready output whose body is the imported source media."""

    owns_root = root is None
    base = Path(tempfile.mkdtemp(prefix="nle-semantic-import-")) if root is None else root
    base.mkdir(parents=True, exist_ok=True)
    body = build_imported_source_media(ffmpeg, base / "imported-source.mp4")
    production, projection, authoring, authoring_projection, history, _store = (
        accepted._registries_with_ready_output(  # noqa: SLF001 - the accepted bootstrap
            base,
            segment_count=segments,
            artifact_bodies=(body,) * segments,
            frame_count=IMPORTED_SOURCE_FRAME_COUNT,
            width=SOURCE_WIDTH,
            height=SOURCE_HEIGHT,
        )
    )
    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=base / "authoring-scratch",
        clock_ms=clock_ms or (lambda: max(1, time.monotonic_ns() // 1_000_000)),
    )
    service = ProductionAuthoringImportService(
        production_registry=production,
        authoring_registry=authoring,
        media_runtime=lambda: adapter,
    )
    return ImportScenario(
        root=base,
        production=production,
        projection=projection,
        authoring=authoring,
        authoring_projection=authoring_projection,
        history=history,
        service=service,
        adapter=adapter,
        source_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
        source_byte_length=len(body),
        _owns_root=owns_root,
    )


# ---------------------------------------------------------------------------------------------
# Driving the scenario through the real registries (the render stage's half).
# ---------------------------------------------------------------------------------------------


def _action(request_id: str, action: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": AUTHORING_ACTION_SCHEMA,
        "request_id": request_id,
        "action": action,
        "payload": dict(payload),
    }


def read_history(scenario: ImportScenario) -> dict[str, Any]:
    handle = scenario.authoring_projection["workspace_handle"]
    result = scenario.authoring.dispatch(
        _action(
            f"nle-import-read-{time.monotonic_ns()}",
            "read_timeline_history",
            {"workspace_handle": handle},
        )
    )
    if result.status != 200 or not isinstance(result.body, Mapping):
        raise ImportScenarioError("history_unreadable")
    return cast(dict[str, Any], result.body)


def timeline_state(history: Mapping[str, Any]) -> dict[str, Any]:
    """Return the current editable state without pretending an empty V2 timeline can render."""

    legacy_snapshot = history.get("snapshot")
    if isinstance(legacy_snapshot, Mapping):
        return cast(dict[str, Any], legacy_snapshot)
    authoring = history.get("authoring")
    if isinstance(authoring, Mapping) and "render_snapshot" in history:
        return cast(dict[str, Any], authoring)
    raise ImportScenarioError("history_projection_schema_unsupported")


def render_snapshot(history: Mapping[str, Any]) -> dict[str, Any]:
    """Return only a materialized V1 render subject from either history projection version."""

    legacy_snapshot = history.get("snapshot")
    if isinstance(legacy_snapshot, Mapping):
        return cast(dict[str, Any], legacy_snapshot)
    materialized = history.get("render_snapshot")
    if isinstance(materialized, Mapping):
        return cast(dict[str, Any], materialized)
    raise ImportScenarioError("render_snapshot_unavailable")


def inserted_clip_wire(snapshot: Mapping[str, Any], asset_id: str) -> dict[str, Any]:
    """Mirror the product Add control's source-duration and remaining-capacity rules."""

    taken = {clip["clip_id"] for clip in snapshot["clips"]}
    base = f"clip-r{snapshot['timeline_revision']}"
    index = 1
    while f"{base}-{index}" in taken:
        index += 1
    tracks = [track for track in snapshot["tracks"] if track.get("kind") == "primary_video"]
    if not tracks or not tracks[0].get("enabled") or tracks[0].get("locked"):
        raise ImportScenarioError("primary_video_track_unavailable")
    start_frame = snapshot.get("content_end_exclusive")
    edit_capacity = snapshot.get("edit_capacity_frames")
    if (
        type(start_frame) is not int
        or type(edit_capacity) is not int
        or start_frame < 0
        or edit_capacity < 1
        or start_frame >= edit_capacity
    ):
        raise ImportScenarioError("edit_capacity_unavailable")
    asset = next(
        (row for row in snapshot["assets"] if row.get("asset_id") == asset_id),
        None,
    )
    if asset is None:
        raise ImportScenarioError("inserted_asset_absent")
    source_duration = _media_insertion_duration_frames(asset)
    duration_frames = min(source_duration, edit_capacity - start_frame)
    return {
        "clip_id": f"{base}-{index}",
        "asset_id": asset_id,
        "track_id": tracks[0]["track_id"],
        "start_frame": start_frame,
        "duration_frames": duration_frames,
        "source_start_frame": INSERTED_CLIP_SOURCE_START_FRAME,
        "enabled": True,
        "transform": {
            "anchor_x_bp": 5000,
            "anchor_y_bp": 5000,
            "position_x_bp": 0,
            "position_y_bp": 0,
            "scale_x_bp": 10000,
            "scale_y_bp": 10000,
            "rotation_mdeg": 0,
        },
        "crop": {"left_bp": 0, "top_bp": 0, "right_bp": 0, "bottom_bp": 0},
        "opacity_bp": 10000,
        "blend": "normal",
        "text": None,
        "transition": {"kind": "none", "duration_frames": 0},
        "effect": {
            "kind": "none",
            "brightness_permille": 0,
            "contrast_permille": 1000,
            "saturation_permille": 1000,
        },
    }


def _media_insertion_duration_frames(asset: Mapping[str, Any]) -> int:
    """Match ``mediaInsertionDurationFrames`` for the product's fixed 24 fps authoring rate."""

    kind = asset.get("kind")
    if kind == "image":
        return 1
    if kind != "video" or type(asset.get("source_frame_count")) is not int:
        raise ImportScenarioError("insert_source_duration_unavailable")
    if asset["source_frame_count"] < 1:
        raise ImportScenarioError("insert_source_duration_unavailable")
    time_base = asset.get("source_time_base")
    landmarks = asset.get("landmarks")
    if (
        not isinstance(time_base, Mapping)
        or type(time_base.get("num")) is not int
        or type(time_base.get("den")) is not int
        or time_base["num"] < 1
        or time_base["den"] < 1
        or not isinstance(landmarks, (list, tuple))
        or not landmarks
        or not isinstance(landmarks[0], Mapping)
        or not isinstance(landmarks[-1], Mapping)
    ):
        raise ImportScenarioError("insert_source_duration_unavailable")
    first = landmarks[0]
    last = landmarks[-1]
    if (
        first.get("frame_index") != 0
        or type(first.get("pts")) is not int
        or type(last.get("pts")) is not int
        or type(last.get("duration_ticks")) is not int
        or last["duration_ticks"] < 1
    ):
        raise ImportScenarioError("insert_source_duration_unavailable")
    source_ticks = int(last["pts"]) + int(last["duration_ticks"]) - int(first["pts"])
    numerator = source_ticks * int(time_base["num"]) * AUTHORING_FRAME_RATE_NUM
    denominator = int(time_base["den"]) * AUTHORING_FRAME_RATE_DEN
    if source_ticks < 1 or denominator < 1:
        raise ImportScenarioError("insert_source_duration_unavailable")
    frames = numerator // denominator
    if frames < 1:
        raise ImportScenarioError("insert_source_duration_unavailable")
    return frames


def apply_commands(
    scenario: ImportScenario,
    history: Mapping[str, Any],
    request_id: str,
    commands: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply through the exact history version the product returned for this workspace."""

    state = timeline_state(history)
    if isinstance(history.get("authoring"), Mapping):
        payload = {
            "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
            "authoring_schema": NLE_AUTHORING_SCHEMA,
            "profile_id": NLE_AUTHORING_PROFILE_ID,
            "operation_profile_id": NLE_OPERATION_PROFILE_ID,
            "request_id": request_id,
            "transaction_id": f"tx-{request_id}",
            "workspace_handle": state["workspace_handle"],
            "expected_workspace_revision": state["workspace_revision"],
            "expected_timeline_revision": state["timeline_revision"],
            "expected_timeline_fingerprint": state["timeline_fingerprint"],
            "expected_authoring_fingerprint": state["authoring_fingerprint"],
            "commands": commands,
        }
    else:
        payload = {
            "schema": TIMELINE_TRANSACTION_SCHEMA,
            "request_id": request_id,
            "transaction_id": f"tx-{request_id}",
            "workspace_handle": state["workspace_handle"],
            "expected_workspace_revision": state["workspace_revision"],
            "expected_timeline_revision": state["timeline_revision"],
            "expected_timeline_fingerprint": state["timeline_fingerprint"],
            "commands": commands,
        }
    result = scenario.authoring.dispatch(_action(request_id, "apply_timeline_transaction", payload))
    if result.status != 200:
        raise ImportScenarioError(f"transaction_{result.status}")
    return read_history(scenario)


def landmark_table_sha256(asset: Mapping[str, Any]) -> str:
    """A digest of the measured frame table: `[[frame_index, pts], ...]` as compact JSON.

    The browser journey computes the same digest from the decoded snapshot's landmarks
    (`JSON.stringify(landmarks.map((l) => [l.frameIndex, l.pts]))`), so the two halves can be
    held to the same measured table without either shipping the table into its facts.
    """

    table = [[int(row["frame_index"]), int(row["pts"])] for row in asset.get("landmarks") or ()]
    return hashlib.sha256(json.dumps(table, separators=(",", ":")).encode()).hexdigest()


@dataclass(slots=True)
class ImportSteps:
    """Every identity the scenario minted, in the order it minted them."""

    gesture: str
    imported_asset_id: str
    receipt: dict[str, Any]
    source_fingerprint: str
    pre_import: dict[str, Any]
    post_import: dict[str, Any]
    post_insert: dict[str, Any]
    inserted_clip: dict[str, Any]
    imported_asset: dict[str, Any]

    def facts(self) -> dict[str, Any]:
        pre_import_state = timeline_state(self.pre_import)
        post_import_state = timeline_state(self.post_import)
        post_insert_state = timeline_state(self.post_insert)
        post_insert_snapshot = render_snapshot(self.post_insert)
        output = post_insert_snapshot["output"]
        return {
            "gesture": self.gesture,
            "imported_asset_id": self.imported_asset_id,
            "source_fingerprint": self.source_fingerprint,
            "imported_source_frame_count": self.imported_asset.get("source_frame_count"),
            "imported_source_landmark_table_sha256": landmark_table_sha256(self.imported_asset),
            "output_width": output["width"],
            "output_height": output["height"],
            "output_duration_frames": output["duration_frames"],
            "post_import_clip_count": len(post_import_state["clips"]),
            "post_insert_clip_count": len(post_insert_state["clips"]),
            "receipt_rows": len(self.receipt.get("rows", ())),
            "receipt_asset_ids": [row["asset_id"] for row in self.receipt.get("rows", ())],
            "pre_import_timeline_revision": pre_import_state["timeline_revision"],
            "post_import_timeline_revision": post_import_state["timeline_revision"],
            "import_changed_timeline_revision": (
                post_import_state["timeline_revision"] != pre_import_state["timeline_revision"]
            ),
            "import_advanced_workspace_revision": (
                post_import_state["workspace_revision"] > pre_import_state["workspace_revision"]
            ),
            "import_changed_timeline_fingerprint": (
                post_import_state["timeline_fingerprint"]
                != pre_import_state["timeline_fingerprint"]
            ),
            "post_insert_public_fingerprint": post_insert_snapshot["public_fingerprint"],
            "post_insert_timeline_fingerprint": post_insert_snapshot["timeline_fingerprint"],
            "post_insert_timeline_revision": post_insert_state["timeline_revision"],
            "inserted_clip_id": self.inserted_clip["clip_id"],
            "inserted_clip_track_id": self.inserted_clip["track_id"],
            "inserted_clip_start_frame": self.inserted_clip["start_frame"],
            "inserted_clip_duration_frames": self.inserted_clip["duration_frames"],
            "inserted_clip_source_start_frame": self.inserted_clip["source_start_frame"],
            "imported_source_landmarks_match": imported_source_landmarks_match(self.imported_asset),
        }


def import_and_insert(scenario: ImportScenario, *, gesture: str) -> ImportSteps:
    """Import the ready output through the real service, then insert the minted asset once."""

    pre_import = read_history(scenario)
    request = accepted._import_request(  # noqa: SLF001
        scenario.projection,
        scenario.authoring_projection,
        pre_import,
        request_id=f"nle-semantic-import-{gesture}",
    )
    response = scenario.service.dispatch(
        request, deadline=time.monotonic() + IMPORT_DEADLINE_SECONDS
    )
    wire = response.to_wire()
    receipt = cast(dict[str, Any], wire["receipt"])
    rows = list(receipt.get("rows") or ())
    if len(rows) != 1:
        raise ImportScenarioError("import_receipt_rows")
    asset_id = str(rows[0]["asset_id"])
    post_import = read_history(scenario)
    post_import_state = timeline_state(post_import)
    if post_import_state["clips"]:
        raise ImportScenarioError("import_mutated_timeline")
    imported_asset = next(
        (a for a in post_import_state["assets"] if a.get("asset_id") == asset_id), None
    )
    if imported_asset is None:
        raise ImportScenarioError("imported_asset_absent")
    clip = inserted_clip_wire(post_import_state, asset_id)
    post_insert = apply_commands(
        scenario,
        post_import,
        f"nle-semantic-insert-{gesture}",
        [{"kind": "insert_asset_clip", "payload": {"clip": clip}}],
    )
    placed = [c for c in timeline_state(post_insert)["clips"] if c["asset_id"] == asset_id]
    if len(placed) != 1:
        raise ImportScenarioError("insertion_count")
    return ImportSteps(
        gesture=gesture,
        imported_asset_id=asset_id,
        receipt=receipt,
        source_fingerprint=scenario.source_fingerprint,
        pre_import=pre_import,
        post_import=post_import,
        post_insert=post_insert,
        inserted_clip=clip,
        imported_asset=cast(dict[str, Any], imported_asset),
    )


#: The snapshot fields that are the composition's content. Undo and redo are judged on these
#: alone: the product's timeline fingerprint folds the revision in, so a history step that
#: restores the exact same composition never restores the same fingerprint, and comparing
#: fingerprints would report a correct undo as a failure (the browser journey applies the same
#: rule from ``nleSemanticShellEvidence.ts``).
TIMELINE_CONTENT_FIELDS = ("output", "capability", "assets", "tracks", "clips", "audio_extension")


def timeline_content(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {field: snapshot.get(field) for field in TIMELINE_CONTENT_FIELDS}


@dataclass(slots=True)
class HistorySteps:
    post_undo: dict[str, Any]
    post_redo: dict[str, Any]

    def facts(self, steps: ImportSteps) -> dict[str, Any]:
        post_undo_state = timeline_state(self.post_undo)
        post_redo_state = timeline_state(self.post_redo)
        post_undo_fingerprint = post_undo_state.get("timeline_fingerprint")
        post_redo_snapshot = render_snapshot(self.post_redo)
        post_insert_snapshot = render_snapshot(steps.post_insert)
        library_after_undo = {a["asset_id"] for a in post_undo_state["assets"]}
        return {
            "post_undo_timeline_fingerprint": post_undo_fingerprint,
            "post_undo_timeline_revision": post_undo_state["timeline_revision"],
            "post_undo_clip_count": len(post_undo_state["clips"]),
            "undo_restored_pre_insert_timeline": (
                timeline_content(post_undo_state)
                == timeline_content(timeline_state(steps.post_import))
            ),
            "library_retained_after_undo": steps.imported_asset_id in library_after_undo,
            "post_redo_public_fingerprint": post_redo_snapshot["public_fingerprint"],
            "post_redo_timeline_revision": post_redo_state["timeline_revision"],
            "post_redo_clip_count": len(post_redo_state["clips"]),
            "redo_restored_post_insert_timeline": (
                timeline_content(post_redo_snapshot) == timeline_content(post_insert_snapshot)
            ),
        }


def all_facts(steps: ImportSteps, history: HistorySteps) -> dict[str, Any]:
    """Every fact the row records, checked to carry each identity the join will require."""

    facts = {**steps.facts(), **history.facts(steps)}
    absent = [key for key in IMPORT_IDENTITY_FACTS if key not in facts]
    if absent:
        raise ImportScenarioError("identity_facts_absent:" + ",".join(absent))
    return facts


def undo_and_redo(scenario: ImportScenario, steps: ImportSteps) -> HistorySteps:
    """Undo the insertion across the pre-import history, then redo it, through the real history."""

    current = read_history(scenario)
    cursor = current.get("undo_cursor")
    if not isinstance(cursor, str):
        raise ImportScenarioError("undo_cursor_absent")
    post_undo = apply_commands(
        scenario,
        current,
        f"nle-semantic-undo-{steps.gesture}",
        [{"kind": "undo", "payload": {"history_cursor": cursor}}],
    )
    redo_cursor = post_undo.get("redo_cursor")
    if not isinstance(redo_cursor, str):
        raise ImportScenarioError("redo_cursor_absent")
    post_redo = apply_commands(
        scenario,
        post_undo,
        f"nle-semantic-redo-{steps.gesture}",
        [{"kind": "redo", "payload": {"history_cursor": redo_cursor}}],
    )
    return HistorySteps(post_undo=post_undo, post_redo=post_redo)
