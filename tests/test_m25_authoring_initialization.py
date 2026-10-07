from __future__ import annotations

import json
import threading
from typing import Any, cast

import pytest

from comfyui_h3_context.adapters import authoring_source_binding as binding
from comfyui_h3_context.adapters.authoring_render_source import PreparedAuthoringHistory
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AUTHORING_ACTION_SCHEMA,
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
    decode_authoring_action_json,
)
from comfyui_h3_context.adapters.comfyui_sidebar_workspace import (
    SidebarAuthoringSeed,
    SidebarAuthoringSource,
    SidebarAuthoringWorkspaceClaim,
)
from comfyui_h3_context.context_request_nodes import H3ReferenceRegistryNode
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.contracts import MediaKind, TaskMode
from comfyui_h3_context.core.nle_authoring_contract import (
    NLE_AUTHORING_PROFILE_ID,
    NLE_AUTHORING_SCHEMA,
    NLE_OPERATION_PROFILE_ID,
    TIMELINE_TRANSACTION_SCHEMA_V2,
)


def image_workspace(
    monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[
    AuthoringWorkspaceRegistry, dict[str, Any], binding.AuthoringSourceBindingReceipt | None
]:
    torch = pytest.importorskip("torch")
    store = binding.ProcessLocalAuthoringSourceBindingStore(
        factory=binding.RuntimeComfySourceFactory()
    )
    monkeypatch.setattr(binding, "_PROCESS_BINDINGS", store)
    registry = H3ReferenceRegistryNode().build_registry(images=torch.zeros((1, 2, 2, 3)))[0]
    receipt = store.claim(registry)
    seed = SidebarAuthoringSeed(
        source_id="report-image",
        task_mode=TaskMode.I2VA,
        registry_fingerprint=canonical_fingerprint({"registry": "image"}),
        sources=(
            SidebarAuthoringSource(
                asset_id="image_1",
                kind=MediaKind.IMAGE,
                duration_milliseconds=None,
                paired_video_id=None,
                connection_order=1,
                identity_fingerprint=canonical_fingerprint({"asset": "image_1"}),
            ),
        ),
    )
    workspace = AuthoringWorkspaceRegistry(
        workspace_claim=lambda _: SidebarAuthoringWorkspaceClaim(seed, receipt),
        **kwargs,
    )
    created = workspace.dispatch(
        action("create", "create_authoring_workspace", context_workspace_handle="context-image")
    )
    assert created.body is not None
    return workspace, created.body, receipt


def action(request: str, name: str, **payload: object) -> dict[str, Any]:
    return {
        "schema": AUTHORING_ACTION_SCHEMA,
        "request_id": request,
        "action": name,
        "payload": payload,
    }


def initialize(projection: dict[str, Any], request: str = "init") -> dict[str, Any]:
    return action(
        request,
        "initialize_timeline_history",
        workspace_handle=projection["workspace_handle"],
        expected_reference_revision=projection["reference"]["revision"],
        expected_timeline_revision=projection["timeline"]["revision"],
        authoring_schema=NLE_AUTHORING_SCHEMA,
        profile_id=NLE_AUTHORING_PROFILE_ID,
        operation_profile_id=NLE_OPERATION_PROFILE_ID,
    )


def insert_image_clip(
    workspace: AuthoringWorkspaceRegistry,
    authoring_state: dict[str, Any],
    *,
    request_id: str,
    clip_id: str,
) -> dict[str, Any]:
    track_id = "image-track-" + clip_id
    transform = {
        "anchor_x_bp": 5000,
        "anchor_y_bp": 5000,
        "position_x_bp": 0,
        "position_y_bp": 0,
        "scale_x_bp": 10000,
        "scale_y_bp": 10000,
        "rotation_mdeg": 0,
    }
    transaction = {
        "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
        "authoring_schema": NLE_AUTHORING_SCHEMA,
        "profile_id": NLE_AUTHORING_PROFILE_ID,
        "operation_profile_id": NLE_OPERATION_PROFILE_ID,
        "request_id": request_id,
        "transaction_id": "tx-" + request_id,
        "workspace_handle": authoring_state["workspace_handle"],
        "expected_workspace_revision": authoring_state["workspace_revision"],
        "expected_timeline_revision": authoring_state["timeline_revision"],
        "expected_timeline_fingerprint": authoring_state["timeline_fingerprint"],
        "expected_authoring_fingerprint": authoring_state["authoring_fingerprint"],
        "commands": [
            {
                "kind": "create_track",
                "payload": {"track_id": track_id, "kind": "image_overlay", "order": 1},
            },
            {
                "kind": "insert_asset_clip",
                "payload": {
                    "clip": {
                        "clip_id": clip_id,
                        "asset_id": "image_1",
                        "track_id": track_id,
                        "start_frame": 0,
                        "duration_frames": 12,
                        "source_start_frame": 0,
                        "enabled": True,
                        "transform": transform,
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
                },
            },
        ],
    }
    result = workspace.dispatch(action(request_id, "apply_timeline_transaction", **transaction))
    assert result.status == 200 and result.body is not None
    return cast(dict[str, Any], result.body["authoring"])


def test_initialize_wire_is_closed_and_cannot_carry_source_or_snapshot() -> None:
    wire = action(
        "init",
        "initialize_timeline_history",
        workspace_handle="authoring-one",
        expected_reference_revision=1,
        expected_timeline_revision=1,
        authoring_schema=NLE_AUTHORING_SCHEMA,
        profile_id=NLE_AUTHORING_PROFILE_ID,
        operation_profile_id=NLE_OPERATION_PROFILE_ID,
    )
    assert decode_authoring_action_json(json.dumps(wire).encode()) == wire
    for field in ("snapshot", "assets", "path", "source_binding", "source_generation"):
        payload = dict(wire["payload"], **{field: "untrusted"})
        with pytest.raises(ValueError):
            decode_authoring_action_json(json.dumps(dict(wire, payload=payload)).encode())


def test_exact_image_initialization_retains_unplaced_library_and_retry_keeps_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, projection, receipt = image_workspace(monkeypatch)
    handle = projection["workspace_handle"]
    with pytest.raises(AuthoringWorkbenchError, match="timeline_history_unavailable"):
        workspace.dispatch(action("read-before", "read_timeline_history", workspace_handle=handle))
    initialized = workspace.dispatch(initialize(projection))
    assert initialized.status == 200 and initialized.body is not None
    authoring = cast(dict[str, Any], initialized.body["authoring"])
    assert initialized.body["render_snapshot"] is None
    assert authoring["clips"] == []
    assert {a["asset_id"] for a in authoring["assets"]} == {"image_1", "h3.font.noto_sans.v1"}
    commands = [
        {
            "kind": "create_track",
            "payload": {"track_id": "image-track", "kind": "image_overlay", "order": 1},
        },
        {
            "kind": "insert_asset_clip",
            "payload": {
                "clip": {
                    "clip_id": "image-clip",
                    "asset_id": "image_1",
                    "track_id": "image-track",
                    "start_frame": 0,
                    "duration_frames": 12,
                    "source_start_frame": 0,
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
            },
        },
    ]
    payload = {
        "schema": TIMELINE_TRANSACTION_SCHEMA_V2,
        "authoring_schema": NLE_AUTHORING_SCHEMA,
        "profile_id": NLE_AUTHORING_PROFILE_ID,
        "operation_profile_id": NLE_OPERATION_PROFILE_ID,
        "request_id": "insert",
        "transaction_id": "tx-insert",
        "workspace_handle": handle,
        "expected_workspace_revision": authoring["workspace_revision"],
        "expected_timeline_revision": authoring["timeline_revision"],
        "expected_timeline_fingerprint": authoring["timeline_fingerprint"],
        "expected_authoring_fingerprint": authoring["authoring_fingerprint"],
        "commands": commands,
    }
    result = workspace.dispatch(action("insert", "apply_timeline_transaction", **payload))
    assert result.status == 200
    current = workspace.dispatch(
        action("read-after", "read_timeline_history", workspace_handle=handle)
    )
    replay = workspace.dispatch(initialize(projection))
    assert replay.body == current.body
    assert replay.body is not None
    replay_authoring = cast(dict[str, Any], replay.body["authoring"])
    assert replay_authoring["clips"][0]["clip_id"] == "image-clip"
    assert receipt is not None and not receipt.released


def test_initialization_revision_conflict_leaves_history_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, projection, _ = image_workspace(monkeypatch)
    wire = initialize(projection)
    wire["payload"]["expected_reference_revision"] += 1
    with pytest.raises(AuthoringWorkbenchError, match="initialization_revision_conflict"):
        workspace.dispatch(wire)
    with pytest.raises(AuthoringWorkbenchError, match="timeline_history_unavailable"):
        workspace.dispatch(
            action("read", "read_timeline_history", workspace_handle=projection["workspace_handle"])
        )


def test_bound_planner_uses_owned_source_and_invalidates_after_workspace_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, projection, _ = image_workspace(monkeypatch)
    handle = projection["workspace_handle"]
    initialized = workspace.dispatch(initialize(projection))
    assert initialized.body is not None
    assert initialized.body["render_snapshot"] is None
    empty_state = cast(dict[str, Any], initialized.body["authoring"])
    assert empty_state["content_end_exclusive"] == 0
    placed_state = insert_image_clip(
        workspace,
        empty_state,
        request_id="planner-place-image",
        clip_id="planner-image-clip",
    )
    assert placed_state["content_end_exclusive"] == 12
    prepared = workspace.prepare_render_plan(handle)
    again = workspace.prepare_render_plan(handle)
    assert prepared.plan.idempotency_fingerprint == again.plan.idempotency_fingerprint
    assert prepared.plan.unavailable_disposition == "renderer_unqualified"
    assert prepared.plan.source_bindings[0].origin == "runtime_image"
    assert prepared.confirm_currentness() == prepared.plan.source_currentness_claim
    public = json.dumps(initialized.body)
    for forbidden in (
        "source_fingerprint",
        "source_facts_fingerprint",
        "currentness_token",
        "lease_fingerprint",
        "private_source",
    ):
        assert forbidden not in public
    workspace.dispatch(action("release", "release_workspace", workspace_handle=handle))
    with pytest.raises(binding.AuthoringSourceBindingError, match="source_stale"):
        prepared.confirm_currentness()


def test_release_during_preparation_never_binds_or_blocks_registry_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.authoring_render_source import prepare_authoring_history

    entered = threading.Event()
    resume = threading.Event()
    errors: list[Exception] = []

    def prepare(
        source_projection: dict[str, object],
        source_receipt: binding.AuthoringSourceBindingReceipt | None,
    ) -> PreparedAuthoringHistory:
        candidate = prepare_authoring_history(source_projection, source_receipt)
        entered.set()
        assert resume.wait(5)
        return candidate

    workspace, projection, receipt = image_workspace(monkeypatch, history_preparer=prepare)

    def run() -> None:
        try:
            workspace.dispatch(initialize(projection))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    try:
        result = workspace.dispatch(
            action("release", "release_workspace", workspace_handle=projection["workspace_handle"])
        )
        assert result.status == 204
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert receipt is not None and receipt.released
    assert len(errors) == 1 and isinstance(errors[0], AuthoringWorkbenchError)
    assert errors[0].code == "workspace_gone"


def test_failed_preparation_can_retry_without_releasing_workspace_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.authoring_render_source import prepare_authoring_history

    attempts = 0

    def prepare(
        source_projection: dict[str, object],
        source_receipt: binding.AuthoringSourceBindingReceipt | None,
    ) -> PreparedAuthoringHistory:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise binding.AuthoringSourceBindingError("source_probe_timeout")
        return prepare_authoring_history(source_projection, source_receipt)

    workspace, projection, receipt = image_workspace(monkeypatch, history_preparer=prepare)
    with pytest.raises(AuthoringWorkbenchError, match="source_probe_timeout"):
        workspace.dispatch(initialize(projection))
    assert receipt is not None and not receipt.released
    assert workspace.dispatch(initialize(projection)).status == 200
    assert attempts == 2


def test_concurrent_initialize_is_busy_and_late_source_release_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters.authoring_render_source import prepare_authoring_history

    entered = threading.Event()
    resume = threading.Event()
    provisional: list[PreparedAuthoringHistory] = []
    errors: list[Exception] = []

    def prepare(
        source_projection: dict[str, object],
        source_receipt: binding.AuthoringSourceBindingReceipt | None,
    ) -> PreparedAuthoringHistory:
        candidate = prepare_authoring_history(source_projection, source_receipt)
        provisional.append(candidate)
        entered.set()
        assert resume.wait(5)
        return candidate

    workspace, projection, receipt = image_workspace(monkeypatch, history_preparer=prepare)

    def run() -> None:
        try:
            workspace.dispatch(initialize(projection))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(AuthoringWorkbenchError, match="timeline_initialization_busy"):
            workspace.dispatch(initialize(projection, "concurrent"))
        assert receipt is not None
        receipt.release()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AuthoringWorkbenchError)
    assert errors[0].code == "initialization_currentness_conflict"
    assert provisional[0].sources == () and not provisional[0].current()
    with pytest.raises(AuthoringWorkbenchError, match="timeline_history_unavailable"):
        workspace.dispatch(
            action("read", "read_timeline_history", workspace_handle=projection["workspace_handle"])
        )


def test_foreign_prepared_empty_history_cannot_bind_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from comfyui_h3_context.adapters.authoring_render_source import prepare_authoring_history

    provisional = []

    def prepare(
        source_projection: dict[str, object],
        source_receipt: binding.AuthoringSourceBindingReceipt | None,
    ) -> PreparedAuthoringHistory:
        candidate = prepare_authoring_history(source_projection, source_receipt)
        assert candidate.snapshot is None
        assert candidate.authoring_state is not None
        candidate.authoring_state = replace(
            candidate.authoring_state, workspace_handle="authoring-foreign"
        )
        provisional.append(candidate)
        return candidate

    workspace, projection, receipt = image_workspace(monkeypatch, history_preparer=prepare)
    with pytest.raises(AuthoringWorkbenchError, match="initialization_currentness_conflict"):
        workspace.dispatch(initialize(projection))
    assert provisional[0].snapshot is None
    authoring_state = provisional[0].authoring_state
    assert authoring_state is not None
    assert authoring_state.workspace_handle == "authoring-foreign"
    assert provisional[0].sources == () and not provisional[0].current()
    assert receipt is not None and not receipt.released
