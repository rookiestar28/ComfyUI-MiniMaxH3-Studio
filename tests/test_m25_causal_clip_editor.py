"""M25-08 cross-seam causal acceptance for the selected VIDEO editor path."""

from __future__ import annotations

import json
from abc import ABCMeta
from decimal import Decimal
from pathlib import Path

import pytest

from comfyui_h3_context.adapters.authoring_source_binding import (
    AuthoringSourceBindingError,
    ComfyVideoInputTypeAuthority,
    PathBackedComfyVideoFromFileV1Factory,
)
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringSource,
    SidebarAuthoringWorkspaceClaim,
)
from comfyui_h3_context.core import TaskMode, canonical_fingerprint
from comfyui_h3_context.core.authoring_preview_protocol import AuthoringPreviewRequest
from comfyui_h3_context.core.contracts import AssetRole, MediaKind
from comfyui_h3_context.core.registry import (
    MediaMetadata,
    ReferenceAsset,
    build_reference_registry,
)


def _timeline(body: dict[str, object]) -> dict[str, object]:
    value = body["timeline"]
    assert type(value) is dict
    return value


def _reference(body: dict[str, object]) -> dict[str, object]:
    value = body["reference"]
    assert type(value) is dict
    return value


def test_selected_video_authority_survives_edit_preview_and_release_causally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    source_path = input_root / "fixture.mp4"
    source_path.write_bytes(b"m25-08-synthetic-video")

    exact_registry = build_reference_registry(
        (
            ReferenceAsset(
                "video-1",
                MediaKind.VIDEO,
                AssetRole.REFERENCE,
                1,
                MediaMetadata(duration_seconds=Decimal("5")),
            ),
        )
    )
    video_input = ABCMeta(
        "VideoInput",
        (),
        {"__module__": "comfy_api.latest._input.video_types"},
    )
    video_from_file = type(
        "VideoFromFile",
        (video_input,),
        {"__module__": "comfy_api.latest._input_impl.video_types"},
    )
    video_from_components = type(
        "VideoFromComponents",
        (video_input,),
        {"__module__": "comfy_api.latest._input_impl.video_types"},
    )
    runtime_video = video_from_file()
    object.__setattr__(runtime_video, "_VideoFromFile__file", str(source_path))
    receipt = PathBackedComfyVideoFromFileV1Factory(
        type_authority=ComfyVideoInputTypeAuthority(
            video_input=video_input,
            video_from_file=video_from_file,
            video_from_components=video_from_components,
        ),
        input_root_factory=lambda: input_root,
    ).capture(
        exact_registry=exact_registry,
        generation=1,
        sources=(("video-1", MediaKind.VIDEO, runtime_video),),
    )
    seed = SidebarAuthoringSeed(
        source_id="context-report-1",
        task_mode=TaskMode.T2VA,
        registry_fingerprint=canonical_fingerprint(exact_registry.to_wire()),
        sources=(
            SidebarAuthoringSource(
                asset_id="video-1",
                kind=MediaKind.VIDEO,
                duration_milliseconds=5_000,
                paired_video_id=None,
                connection_order=1,
                identity_fingerprint=canonical_fingerprint(exact_registry.assets[0].to_wire()),
            ),
        ),
    )
    workspace_claims = 0

    def claim_workspace(handle: str) -> SidebarAuthoringWorkspaceClaim:
        nonlocal workspace_claims
        assert handle == "context-workspace-1"
        workspace_claims += 1
        return SidebarAuthoringWorkspaceClaim(seed, receipt)

    registry = AuthoringWorkspaceRegistry(workspace_claim=claim_workspace)

    def action(request_id: str, name: str, **payload: object) -> dict[str, object]:
        return {
            "schema": AUTHORING_ACTION_SCHEMA,
            "request_id": request_id,
            "action": name,
            "payload": payload,
        }

    created = registry.dispatch(
        action(
            "m25-08-create",
            "create_authoring_workspace",
            context_workspace_handle="context-workspace-1",
        )
    )
    assert created.status == 201 and created.body is not None
    assert workspace_claims == 1
    handle = str(created.body["workspace_handle"])
    source_projection = _reference(created.body)["sources"]
    assert type(source_projection) is list and len(source_projection) == 1
    assert source_projection[0] == {
        "source_id": "video-1",
        "kind": "video",
        "admitted": True,
        "admissible": True,
        "reason": None,
        "duration_milliseconds": 5_000,
        "label": "<Video 1>",
        "paired_with": None,
        "derived_soundtrack": "excluded",
        "preview": {
            "schema": "h3.context.authoring_source_preview.capability.v1",
            "available": True,
            "reason": None,
        },
    }

    added = registry.dispatch(
        action(
            "m25-08-add",
            "add_clip",
            workspace_handle=handle,
            expected_timeline_revision=_timeline(created.body)["revision"],
            asset_id="video-1",
            lane=0,
            start_frame=24,
            frames=48,
            source_start_frame=12,
        )
    )
    assert added.status == 200 and added.body is not None
    pre_move_revision = _timeline(added.body)["revision"]
    moved = registry.dispatch(
        action(
            "m25-08-move",
            "move_clip",
            workspace_handle=handle,
            expected_timeline_revision=pre_move_revision,
            clip_id="clip-1",
            delta_frames=24,
            delta_lanes=0,
        )
    )
    assert moved.status == 200 and moved.body is not None
    accepted_timeline = _timeline(moved.body)
    assert accepted_timeline["revision"] == int(str(pre_move_revision)) + 1
    assert accepted_timeline["clips"][0]["start_frame"] == 48  # type: ignore[index]

    stale = registry.dispatch(
        action(
            "m25-08-stale",
            "move_clip",
            workspace_handle=handle,
            expected_timeline_revision=pre_move_revision,
            clip_id="clip-1",
            delta_frames=24,
            delta_lanes=0,
        )
    )
    assert stale.status == 409 and stale.body is not None
    assert stale.body["rejection"] == {"code": "stale_revision"}
    assert _timeline(stale.body) == accepted_timeline

    request = AuthoringPreviewRequest(
        request_id="m25-08-preview",
        workspace_handle=handle,
        reference_revision=int(str(_reference(moved.body)["revision"])),
        timeline_revision=int(str(accepted_timeline["revision"])),
        timeline_content_fingerprint=str(accepted_timeline["content_fingerprint"]),
        clip_id="clip-1",
    )
    preview = registry.admit_media_preview(request)
    assert registry.media_preview_is_current(preview)
    assert (preview.source_id, preview.source_start_frame, preview.frames, preview.video_fps) == (
        "video-1",
        12,
        48,
        24,
    )

    adapter = object.__new__(QualifiedAVMediaAdapter)
    captured: dict[str, object] = {}

    def execute(_self: QualifiedAVMediaAdapter, **kwargs: object) -> tuple[bytearray, str]:
        captured.update(kwargs)
        return bytearray(b"normalized-mp4"), "present_bound"

    monkeypatch.setattr(QualifiedAVMediaAdapter, "execute_authoring_preview", execute)
    body, audio = preview.render(adapter, deadline=10.0)
    assert (body, audio) == (bytearray(b"normalized-mp4"), "present_bound")
    assert captured["source_path"] == source_path
    assert captured["source_start_frame"] == 12
    assert captured["frames"] == 48

    public_evidence = json.dumps(
        {"created": created.body, "accepted": moved.body, "stale": stale.body},
        sort_keys=True,
    )
    assert str(source_path) not in public_evidence
    assert "fixture.mp4" not in public_evidence
    assert repr(receipt).find(str(source_path)) == -1
    assert repr(preview) == "<AuthoringMediaPreviewClaim opaque>"

    released = registry.dispatch(
        action("m25-08-release", "release_workspace", workspace_handle=handle)
    )
    assert released.status == 204
    assert receipt.released
    assert not registry.media_preview_is_current(preview)
    with pytest.raises(AuthoringSourceBindingError, match="source_stale"):
        preview.render(adapter, deadline=10.0)
