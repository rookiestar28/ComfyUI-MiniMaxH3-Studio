"""Real job/store lifecycle through a synthetic renderer, not media qualification."""

from __future__ import annotations

import importlib
import importlib.util
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_m25_authoring_render_jobs import SyntheticLifecycleBackend
from test_m25_render_job_leases import image_bound as image_bound

from comfyui_h3_context.adapters.authoring_render_service import AuthoringRenderService
from comfyui_h3_context.adapters.authoring_render_source import (
    PreparedAuthoringHistory,
    prepare_bound_render_plan,
)
from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AuthoringDispatchResult,
    AuthoringWorkbenchError,
)
from comfyui_h3_context.core.authoring_output_protocol import (
    OUTPUT_CREATE_SCHEMA,
    AuthoringOutputCreate,
    OutputProtocolError,
)
from comfyui_h3_context.core.composition_contract import (
    OUTPUT_PROFILE_ID,
    decode_public_snapshot,
    public_snapshot_fingerprint,
)
from comfyui_h3_context.core.nle_authoring_contract import (
    TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
)


def outputs() -> Any:
    name = "comfyui_h3_context.adapters.authoring_output_service"
    assert importlib.util.find_spec(name) is not None, "public output authority is missing"
    return importlib.import_module(name)


def test_public_output_authority_contract_is_present() -> None:
    assert callable(outputs().AuthoringOutputRegistry)


def test_reference_and_history_revision_are_independent(subject: Any) -> None:
    registry, request, workspace, *_rest = subject
    workspace.reference_revision += 4
    status = completed(registry, request)
    assert status["currency"] == "current"
    workspace.reference_revision += 1
    after = registry.status(status["job_handle"], request.workspace_handle)
    assert after["currency"] == "old_revision"


def test_reference_change_during_preparation_refuses_submission(
    subject: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, request, workspace, _service, _store, backend, _now = subject
    original = workspace.prepare_render_plan

    def changed(handle: str) -> Any:
        bound = original(handle)
        workspace.reference_revision += 1
        return bound

    monkeypatch.setattr(workspace, "prepare_render_plan", changed)
    with pytest.raises(OutputProtocolError, match="revision_conflict"):
        registry.create(request)
    assert backend.calls == 0


class WorkspaceDouble:
    """Workspace port only; source claims, planner, render jobs and store remain real."""

    def __init__(self, bound: Any) -> None:
        self.bound = bound
        self.snapshot = bound._issued_snapshot
        self.reference_revision = self.snapshot.workspace_revision
        self.live = True
        self.preparations = 0
        self.v2_history = False
        self.v2_render_snapshot: dict[str, object] | None = self.snapshot.to_wire()

    def dispatch(self, action: dict[str, object]) -> AuthoringDispatchResult:
        if not self.live:
            raise AuthoringWorkbenchError(410, "workspace_gone")
        if action["action"] == "read_projection":
            return AuthoringDispatchResult(
                200, {"reference": {"revision": self.reference_revision}}
            )
        assert action["action"] == "read_timeline_history"
        if self.v2_history:
            return AuthoringDispatchResult(
                200,
                {
                    "schema": TIMELINE_HISTORY_PROJECTION_SCHEMA_V2,
                    "authoring": {"workspace_handle": self.snapshot.workspace_handle},
                    "render_snapshot": self.v2_render_snapshot,
                },
            )
        return AuthoringDispatchResult(200, {"snapshot": self.snapshot.to_wire()})

    def prepare_render_plan(self, handle: str) -> Any:
        assert handle == self.snapshot.workspace_handle
        if not self.live:
            raise AuthoringWorkbenchError(410, "workspace_gone")
        self.preparations += 1
        return self.bound


@pytest.fixture
def subject(tmp_path: Path, image_bound: Any) -> Any:
    module = outputs()
    old = image_bound[5]
    wire = old._issued_snapshot.to_wire()
    wire["workspace_handle"] = "authoring-" + "a" * 32
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    snapshot = decode_public_snapshot(wire)
    history = PreparedAuthoringHistory(
        snapshot, old._history.generation, old._history.sources, old._history.fonts
    )
    bound = prepare_bound_render_plan(history, snapshot, lambda: image_bound[6][0])
    workspace = WorkspaceDouble(bound)
    now = [time.monotonic()]
    store = RenderOutputStore(tmp_path, clock=lambda: now[0])
    backend = SyntheticLifecycleBackend(blocked=False)
    service = AuthoringRenderService(store, backend=backend)
    registry = module.AuthoringOutputRegistry(
        workspace=workspace, service=service, store=store, clock=lambda: now[0]
    )
    request = AuthoringOutputCreate(
        schema=OUTPUT_CREATE_SCHEMA,
        workspace_handle=snapshot.workspace_handle,
        workspace_revision=snapshot.workspace_revision,
        timeline_revision=snapshot.timeline_revision,
        snapshot_fingerprint=snapshot.public_fingerprint,
        output_profile_id=OUTPUT_PROFILE_ID,
        idempotency_key="output-request-0001",
    )
    try:
        yield registry, request, workspace, service, store, backend, now
    finally:
        backend.proceed.set()
        registry.close()
        assert store.retained_count == 0


def completed(registry: Any, request: AuthoringOutputCreate) -> dict[str, Any]:
    created = registry.create(request)
    end = time.monotonic() + 4
    while time.monotonic() < end:
        status = registry.status(created["job_handle"], request.workspace_handle)
        if status["phase"] == "succeeded":
            return cast(dict[str, Any], status)
        assert status["phase"] not in {"failed", "cancelled"}, status
        time.sleep(0.01)
    pytest.fail("bounded synthetic lifecycle did not complete")


def test_output_create_calls_real_plan_service_and_keeps_private_joins_out(subject: Any) -> None:
    registry, request, workspace, _service, _store, backend, _now = subject
    status = completed(registry, request)
    assert workspace.preparations == backend.calls == 1
    assert status["job_handle"].startswith("arj_")
    assert status["output_handle"].startswith("aro_")
    assert status["currency"] == "current"
    assert status["availability"] == "available"
    assert status["output"]["audio_streams"] == 0
    assert status["output"]["verified"] is True
    assert (
        not {"receipt", "request", "job_id", "plan_fingerprint", "source_manifest_fingerprint"}
        & status.keys()
    )


def test_output_registry_accepts_materialized_v2_history(subject: Any) -> None:
    registry, request, workspace, _service, _store, backend, _now = subject
    workspace.v2_history = True
    status = completed(registry, request)
    assert status["phase"] == "succeeded"
    assert status["availability"] == "available"
    assert workspace.preparations == backend.calls == 1


def test_output_registry_refuses_empty_v2_history_before_submission(subject: Any) -> None:
    registry, request, workspace, service, store, backend, _now = subject
    workspace.v2_history = True
    workspace.v2_render_snapshot = None
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.create(request)
    assert workspace.preparations == backend.calls == 0
    assert service.job_count == store.staging_count == 0


def test_idempotent_create_after_source_release_does_not_replan_or_enqueue(subject: Any) -> None:
    registry, request, workspace, _service, _store, backend, _now = subject
    first = completed(registry, request)
    workspace.bound._history.sources[0]._receipt.release()
    replay = registry.create(request)
    assert replay["job_handle"] == first["job_handle"]
    assert replay["output_handle"] == first["output_handle"]
    assert workspace.preparations == backend.calls == 1
    with pytest.raises(OutputProtocolError, match="idempotency_conflict"):
        registry.create(replace(request, timeline_revision=request.timeline_revision + 1))


def test_create_after_source_revocation_is_typed_and_allocates_nothing(
    subject: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, request, workspace, service, store, backend, _now = subject
    bound = workspace.bound

    def revoked(handle: str) -> Any:
        # The real planner refusal a released imported Production original produces.
        assert handle == request.workspace_handle
        return prepare_bound_render_plan(bound._history, bound._issued_snapshot, lambda: False)

    monkeypatch.setattr(workspace, "prepare_render_plan", revoked)
    with pytest.raises(OutputProtocolError) as refused:
        registry.create(request)
    assert (refused.value.code, refused.value.status) == ("unavailable", 404)
    assert service.job_count == store.staging_count == backend.calls == 0


@pytest.mark.parametrize(
    "field", ["workspace_revision", "timeline_revision", "snapshot_fingerprint"]
)
def test_create_must_match_current_backend_plan_before_allocating(subject: Any, field: str) -> None:
    registry, request, _workspace, service, store, backend, _now = subject
    value: object = (
        "sha256:" + "f" * 64 if field.endswith("fingerprint") else getattr(request, field) + 1
    )
    with pytest.raises(OutputProtocolError, match="revision_conflict"):
        registry.create(replace(request, **{field: value}))
    assert service.job_count == store.staging_count == backend.calls == 0


def test_old_revision_retained_output_remains_explicitly_downloadable(subject: Any) -> None:
    registry, request, workspace, _service, _store, _backend, _now = subject
    first = completed(registry, request)
    workspace.reference_revision += 1
    status = registry.status(first["job_handle"], request.workspace_handle)
    assert status["currency"] == "old_revision"
    assert status["availability"] == "available"
    with registry.open_download(first["output_handle"], request.workspace_handle) as lease:
        assert b"".join(lease.chunks()) == b"synthetic-complete-output"
    assert registry.active_responses == 0


def test_wrong_workspace_and_revocation_refuse_before_byte_access(subject: Any) -> None:
    registry, request, workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.status(status["job_handle"], "authoring-" + "b" * 32)
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.open_download(status["output_handle"], "authoring-" + "b" * 32)
    workspace.live = False
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.open_download(status["output_handle"], request.workspace_handle)
    assert registry.active_responses == 0


def test_one_response_budget_exact_range_and_early_close(subject: Any) -> None:
    registry, request, _workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)
    lease = registry.open_download(
        status["output_handle"], request.workspace_handle, range_header="bytes=2-6"
    )
    try:
        assert lease.selection.status == 206
        assert lease.headers["Content-Length"] == "5"
        assert b"".join(lease.chunks()) == b"nthet"
        with pytest.raises(OutputProtocolError, match="resource_limit"):
            registry.open_download(status["output_handle"], request.workspace_handle)
    finally:
        lease.close()
        lease.close()
    assert registry.active_responses == 0


def test_invalid_full_artifact_cannot_be_served_as_an_apparently_valid_range(subject: Any) -> None:
    registry, request, _workspace, _service, store, _backend, _now = subject
    status = completed(registry, request)
    artifact = next(iter(store._artifacts.values()))
    artifact._path.write_bytes(b"synthetic-complete-outpuX")
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.open_download(
            status["output_handle"], request.workspace_handle, range_header="bytes=0-2"
        )
    assert registry.active_responses == 0


def test_public_expiry_is_not_renewed_by_polling_and_never_resurrects_bytes(subject: Any) -> None:
    registry, request, _workspace, _service, _store, _backend, now = subject
    first = completed(registry, request)
    now[0] += 3599
    assert (
        registry.status(first["job_handle"], request.workspace_handle)["availability"]
        == "available"
    )
    now[0] += 1
    assert (
        registry.status(first["job_handle"], request.workspace_handle)["availability"] == "expired"
    )
    with pytest.raises(OutputProtocolError, match="expired"):
        registry.open_download(first["output_handle"], request.workspace_handle)
    with pytest.raises(OutputProtocolError, match="expired"):
        registry.create(request)
    now[0] += 601
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.status(first["job_handle"], request.workspace_handle)


def test_response_checks_revocation_and_deadline_without_leaking_body(subject: Any) -> None:
    registry, request, workspace, _service, _store, _backend, now = subject
    status = completed(registry, request)
    with registry.open_download(status["output_handle"], request.workspace_handle) as lease:
        workspace.live = False
        with pytest.raises(OutputProtocolError, match="unavailable"):
            next(lease.chunks())
    workspace.live = True
    with registry.open_download(status["output_handle"], request.workspace_handle) as lease:
        now[0] += 120
        with pytest.raises(OutputProtocolError, match="expired"):
            next(lease.chunks())
    assert registry.active_responses == 0


def test_cancel_keeps_immutable_success_and_exact_public_handle(subject: Any) -> None:
    registry, request, _workspace, _service, _store, _backend, _now = subject
    status = completed(registry, request)
    first = registry.cancel(status["job_handle"], request.workspace_handle)
    second = registry.cancel(status["job_handle"], request.workspace_handle)
    assert first == second == status


def test_preview_reuses_derivative_but_rechecks_parent_and_cleans_up(subject: Any) -> None:
    import hashlib

    from comfyui_h3_context.adapters.authoring_output_preview import preview_dimensions

    registry, request, workspace, _service, store, _backend, _now = subject
    status = completed(registry, request)
    calls = [0]

    class PreviewDouble:
        def render(self, body: bytes, parent: Any, check: Any) -> Any:
            calls[0] += 1
            check()
            assert body
            preview = b"bounded-synthetic-preview"
            width, height = preview_dimensions(parent.width, parent.height)
            facts = replace(
                parent,
                width=width,
                height=height,
                byte_length=len(preview),
                output_fingerprint="sha256:" + hashlib.sha256(preview).hexdigest(),
            )
            return preview, facts

        def close(self) -> None:
            return None

    assert callable(getattr(registry, "open_preview", None)), "output preview admission is missing"
    registry._preview_backend = PreviewDouble()
    for _attempt in range(2):
        with registry.open_preview(status["output_handle"], request.workspace_handle) as lease:
            assert b"".join(lease.chunks()) == b"bounded-synthetic-preview"
            assert lease.headers["Content-Disposition"].startswith("inline;")
            with pytest.raises(OutputProtocolError, match="resource_limit"):
                registry.open_download(status["output_handle"], request.workspace_handle)
    assert calls[0] == 1 and registry.active_responses == 0
    workspace.reference_revision += 1
    with registry.open_preview(status["output_handle"], request.workspace_handle) as lease:
        assert b"".join(lease.chunks())
    assert calls[0] == 1
    artifact = next(iter(store._artifacts.values()))
    artifact._path.unlink()
    with pytest.raises(OutputProtocolError, match="unavailable"):
        registry.open_preview(status["output_handle"], request.workspace_handle)
    assert registry.active_responses == 0
