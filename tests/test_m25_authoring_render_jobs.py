from __future__ import annotations

import hashlib
import importlib
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_m25_render_job_leases import image_bound as image_bound
from test_m25_render_planner import _inputs

from comfyui_h3_context.core.authoring_render_jobs import (
    AuthoringRenderJobRequestV1,
    RenderJobError,
    RenderJobFailure,
    RenderJobLimits,
    RenderJobPhase,
    RenderJobState,
    advance_render_job,
    decode_render_job_request,
    request_for_render_plan,
    require_request_matches_plan,
    semantic_render_fingerprint,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint
from comfyui_h3_context.core.render_planner import RenderPlanV1, plan_render


def _plan() -> RenderPlanV1:
    snapshot, manifest, currentness, fonts = _inputs()
    return plan_render(
        snapshot=snapshot, source_manifest=manifest, currentness=currentness, font_facts=fonts
    )


def _request() -> AuthoringRenderJobRequestV1:
    return request_for_render_plan(_plan(), idempotency_key="request-00000001", timeout_ms=30_000)


def long_plan(*, continuous_image: bool = False) -> RenderPlanV1:
    from test_m25_render_planner import _currentness, _source_manifest

    from comfyui_h3_context.core.composition_contract import (
        decode_public_snapshot,
        public_snapshot_fingerprint,
    )

    snapshot, _, _, fonts = _inputs()
    wire = snapshot.to_wire()
    wire["output"] = replace(snapshot.output, duration_frames=3600).to_wire()
    if continuous_image:
        image_ids = {asset.asset_id for asset in snapshot.assets if asset.kind == "image"}
        clips = wire["clips"]
        assert isinstance(clips, list)
        for clip in clips:
            assert isinstance(clip, dict)
            if clip["asset_id"] in image_ids:
                clip["duration_frames"] = 3600 - clip["start_frame"]
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    expanded = decode_public_snapshot(wire)
    manifest = _source_manifest(expanded)
    return plan_render(
        snapshot=expanded,
        source_manifest=manifest,
        currentness=_currentness(manifest),
        font_facts=fonts,
    )


def test_job_and_semantic_fingerprints_cover_full_3600_frame_plan() -> None:
    plan = long_plan()
    request = request_for_render_plan(plan, idempotency_key="request-long-0001", timeout_ms=30000)
    require_request_matches_plan(request, plan)
    assert semantic_render_fingerprint(plan).startswith("sha256:")


def test_request_joins_large_accepted_plan_without_changing_global_byte_cap() -> None:
    plan = long_plan(continuous_image=True)
    request = request_for_render_plan(plan, idempotency_key="request-large-01", timeout_ms=30000)
    require_request_matches_plan(request, plan)
    assert semantic_render_fingerprint(plan).startswith("sha256:")


def _state() -> RenderJobState:
    return RenderJobState.queued("render-" + "a" * 32, _request().fingerprint)


def services() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_service")


class SyntheticLifecycleBackend:
    """Only service lifecycle evidence; not an executable/capability qualification."""

    def __init__(self, *, blocked: bool = True, fail: bool = False) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()
        if not blocked:
            self.proceed.set()
        self.fail = fail
        self.calls = 0
        self.leases: list[Any] = []
        self.plan: Any = None

    def require_qualified(self, plan: Any, _limits: RenderJobLimits) -> Any:
        return services().RenderExecutionIdentity(
            renderer_fingerprint=canonical_fingerprint({"synthetic-renderer": 1}),
            probe_fingerprint=canonical_fingerprint({"synthetic-probe": 1}),
            qualification_fingerprint=canonical_fingerprint({"unit-only-not-qualified": 1}),
            profile_fingerprint=plan.renderer_capability_profile_fingerprint,
        )

    def render(self, *, plan: Any, sources: Any, stage: Any, control: Any) -> None:
        self.calls += 1
        self.plan = plan
        self.leases.append(sources)
        self.entered.set()
        while not self.proceed.wait(0.01):
            control.check()
        # Deliberately attempt the callback after cancellation to exercise terminal fencing.
        control.progress(RenderJobPhase.RENDERING, 5000)
        control.check()
        if self.fail:
            raise RuntimeError("synthetic-private-diagnostic-never-expose")
        assert sources.read_source("img-overlay")
        stage.output_path.write_bytes(b"synthetic-complete-output")

    def probe(self, *, path: Path, control: Any) -> Any:
        from test_m25_authoring_render_receipts import _facts

        control.check()
        body = path.read_bytes()
        return replace(
            _facts(self.plan),
            byte_length=len(body),
            output_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
        )


def service_store(tmp_path: Path) -> Any:
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore

    return RenderOutputStore(tmp_path)


def await_terminal(service: Any, state: RenderJobState) -> RenderJobState:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        current = service.status(state.job_id, state.request_fingerprint)
        assert isinstance(current, RenderJobState)
        if current.terminal:
            return current
        threading.Event().wait(0.01)
    pytest.fail("bounded synthetic render did not terminalize")


def test_service_default_missing_runtime_refuses_without_allocating(
    tmp_path: Path, image_bound: Any
) -> None:
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        with pytest.raises(services().RenderServiceError, match="runtime_unavailable"):
            service.submit(request, bound)
        assert service.job_count == store.staging_count == store.retained_count == 0
    finally:
        service.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_job_resources_close_before_success_can_be_published(
    tmp_path: Path, image_bound: Any, cleanup_fails: bool
) -> None:
    cleanup_entered = threading.Event()
    cleanup_proceed = threading.Event()
    resource_closed = threading.Event()

    class ResourceBackend(SyntheticLifecycleBackend):
        def render(self, *, plan: Any, sources: Any, stage: Any, control: Any) -> None:
            @contextmanager
            def resource() -> Any:
                descriptor = (
                    os.open(stage.output_path.parent, os.O_RDONLY)
                    if os.name != "nt"
                    else os.open(os.devnull, os.O_RDONLY)
                )
                try:
                    yield descriptor
                finally:
                    cleanup_entered.set()
                    try:
                        assert cleanup_proceed.wait(3)
                    finally:
                        os.close(descriptor)
                        resource_closed.set()
                    if cleanup_fails:
                        raise RuntimeError("synthetic-private-cleanup-diagnostic")

            control.retain_resource(resource())
            super().render(plan=plan, sources=sources, stage=stage, control=control)

    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=ResourceBackend(blocked=False))
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="resource-close-01", timeout_ms=30000
        )
        state = service.submit(request, bound)
        assert cleanup_entered.wait(3)
        assert not service.status(state.job_id, state.request_fingerprint).terminal
        assert store.retained_count == 0
        cleanup_proceed.set()
        final = await_terminal(service, state)
        assert resource_closed.is_set()
        assert final.phase is (RenderJobPhase.FAILED if cleanup_fails else RenderJobPhase.SUCCEEDED)
        assert store.retained_count == (0 if cleanup_fails else 1)
    finally:
        cleanup_proceed.set()
        service.close()


def test_service_idempotent_replay_never_enqueues_a_second_render(
    tmp_path: Path, image_bound: Any
) -> None:
    backend = SyntheticLifecycleBackend()
    service = services().AuthoringRenderService(service_store(tmp_path), backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        first = service.submit(request, bound)
        assert backend.entered.wait(3)
        replay = service.submit(request, bound)
        assert replay.job_id == first.job_id
        with pytest.raises(services().RenderServiceError, match="idempotency_conflict"):
            service.submit(replace(request, timeout_ms=29999), bound)
        assert service.job_count == backend.calls == 1
        backend.proceed.set()
        final = await_terminal(service, first)
        assert final.phase is RenderJobPhase.SUCCEEDED
        artifact = service.artifact(final.job_id, final.request_fingerprint)
        assert artifact.receipt.fingerprint == final.receipt_fingerprint
        assert service.submit(request, bound) == final
    finally:
        backend.proceed.set()
        service.close()
    assert backend.leases[0].source_bytes == 0
    assert list(tmp_path.iterdir()) == []


def test_service_workspace_release_cancels_queue_but_not_handed_off_running_lease(
    tmp_path: Path, image_bound: Any
) -> None:
    backend = SyntheticLifecycleBackend()
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        running = service.submit(request, bound)
        assert backend.entered.wait(3)
        queued = service.submit(replace(request, idempotency_key="request-00000002"), bound)
        image_bound[6][0] = False
        image_bound[3].release()
        failed = await_terminal(service, queued)
        assert failed.failure is RenderJobFailure.WORKSPACE_RELEASED
        backend.proceed.set()
        assert await_terminal(service, running).phase is RenderJobPhase.SUCCEEDED
        assert backend.calls == 1
        assert store.retained_count == 1
    finally:
        backend.proceed.set()
        service.close()


def test_service_queue_budget_and_cancel_fence_late_renderer_callback(
    tmp_path: Path, image_bound: Any
) -> None:
    backend = SyntheticLifecycleBackend()
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        running = service.submit(request, bound)
        assert backend.entered.wait(3)
        queued = [
            service.submit(replace(request, idempotency_key=f"request-0000000{n}"), bound)
            for n in (2, 3)
        ]
        with pytest.raises(services().RenderServiceError, match="resource_limit"):
            service.submit(replace(request, idempotency_key="request-00000004"), bound)
        for state in queued:
            assert (
                service.cancel(state.job_id, state.request_fingerprint).phase
                is RenderJobPhase.CANCELLED
            )
        cancelled = service.cancel(running.job_id, running.request_fingerprint)
        assert cancelled.phase is RenderJobPhase.CANCELLED
        backend.proceed.set()
        assert await_terminal(service, running) == cancelled
        with pytest.raises(services().RenderServiceError, match="output_unavailable"):
            service.artifact(running.job_id, running.request_fingerprint)
    finally:
        backend.proceed.set()
        service.close()
    assert store.retained_count == store.staging_count == 0
    assert backend.calls == 1


def test_service_queued_deadline_elapses_while_worker_is_occupied(
    tmp_path: Path, image_bound: Any
) -> None:
    backend = SyntheticLifecycleBackend()
    service = services().AuthoringRenderService(service_store(tmp_path), backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        service.submit(request, bound)
        assert backend.entered.wait(3)
        queued = service.submit(
            replace(request, idempotency_key="request-00000002", timeout_ms=500), bound
        )
        failed = await_terminal(service, queued)
        assert failed.failure is RenderJobFailure.DEADLINE
        assert backend.calls == 1
    finally:
        backend.proceed.set()
        service.close()


def test_service_renderer_failure_is_closed_and_releases_all_private_resources(
    tmp_path: Path, image_bound: Any
) -> None:
    backend = SyntheticLifecycleBackend(blocked=False, fail=True)
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        state = service.submit(request, bound)
        final = await_terminal(service, state)
        assert final.failure is RenderJobFailure.PROCESS_FAILED
        assert "synthetic-private" not in repr(final.to_wire())
    finally:
        service.close()
    assert backend.leases[0].source_bytes == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kind,code,expected",
    [
        ("process", "cancelled", RenderJobFailure.CANCELLED),
        ("process", "deadline", RenderJobFailure.DEADLINE),
        ("process", "resource_limit", RenderJobFailure.RESOURCE_LIMIT),
        ("process", "runtime_unavailable", RenderJobFailure.RUNTIME_UNAVAILABLE),
        ("process", "session_closed", RenderJobFailure.SERVICE_CLOSED),
        ("graph", "resource_limit", RenderJobFailure.RESOURCE_LIMIT),
        ("probe", "output_invalid", RenderJobFailure.OUTPUT_INVALID),
        ("preparation", "source_invalid", RenderJobFailure.SOURCE_UNAVAILABLE),
        ("preparation", "font_unavailable", RenderJobFailure.FONT_CHANGED),
        ("preparation", "runtime_unavailable", RenderJobFailure.RUNTIME_UNAVAILABLE),
    ],
)
def test_service_preserves_closed_native_boundary_failures(
    tmp_path: Path, image_bound: Any, kind: str, code: str, expected: RenderJobFailure
) -> None:
    from comfyui_h3_context.adapters.authoring_render_executor import RenderPreparationError
    from comfyui_h3_context.adapters.authoring_render_graph import RenderGraphError
    from comfyui_h3_context.adapters.authoring_render_probe import RenderProbeError
    from comfyui_h3_context.adapters.authoring_render_process import RenderProcessError

    errors = {
        "process": RenderProcessError,
        "preparation": RenderPreparationError,
        "graph": RenderGraphError,
    }

    class FailingBoundaryBackend(SyntheticLifecycleBackend):
        def render(self, *, plan: Any, sources: Any, stage: Any, control: Any) -> None:
            self.leases.append(sources)
            stage.write_input(b"synthetic private preparation")
            if kind == "probe":
                raise RenderProbeError()
            raise errors[kind](code)

    backend = FailingBoundaryBackend(blocked=False)
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="native-failure-01", timeout_ms=30000
        )
        final = await_terminal(service, service.submit(request, bound))
        assert final.failure is expected
    finally:
        service.close()
    assert backend.leases[0].source_bytes == 0
    assert list(tmp_path.iterdir()) == []


def test_service_lower_source_budget_refuses_before_job_allocation(
    tmp_path: Path, image_bound: Any
) -> None:
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore

    store = RenderOutputStore(tmp_path, limits=RenderJobLimits(max_source_bytes=47))
    service = services().AuthoringRenderService(store, backend=SyntheticLifecycleBackend())
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        with pytest.raises(services().RenderServiceError, match="resource_limit"):
            service.submit(request, bound)
        assert service.job_count == store.staging_count == 0
    finally:
        service.close()


def test_service_probe_time_replacement_retains_source_failure_not_generic_store_failure(
    tmp_path: Path, image_bound: Any
) -> None:
    from test_m25_render_job_leases import image_source

    from comfyui_h3_context.core.contracts import MediaKind

    class ReplacingBackend(SyntheticLifecycleBackend):
        probes = 0

        def probe(self, *, path: Path, control: Any) -> Any:
            measured = super().probe(path=path, control=control)
            self.probes += 1
            if self.probes == 2:
                image_bound[1].capture(
                    exact_registry=image_bound[2],
                    sources=(("img-overlay", MediaKind.IMAGE, image_source(image_bound[0], 0.75)),),
                )
            return measured

    backend = ReplacingBackend(blocked=False)
    store = service_store(tmp_path)
    service = services().AuthoringRenderService(store, backend=backend)
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        state = service.submit(request, bound)
        final = await_terminal(service, state)
        assert final.failure is RenderJobFailure.SOURCE_REPLACED
        assert store.retained_count == 0
    finally:
        service.close()


def test_service_store_and_terminal_retention_use_one_clock(
    tmp_path: Path, image_bound: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from comfyui_h3_context.adapters.authoring_render_store import RenderOutputStore

    now = [time.monotonic()]
    store = RenderOutputStore(
        tmp_path, limits=RenderJobLimits(retention_seconds=2), clock=lambda: now[0]
    )
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    discard = store.discard

    def held_cleanup(stage: Any) -> None:
        discard(stage)
        cleanup_entered.set()
        assert cleanup_release.wait(5), "test must release the terminal worker"

    monkeypatch.setattr(store, "discard", held_cleanup)
    service = services().AuthoringRenderService(
        store, backend=SyntheticLifecycleBackend(blocked=False)
    )
    try:
        bound = image_bound[5]
        request = request_for_render_plan(
            bound.plan, idempotency_key="request-00000001", timeout_ms=30000
        )
        final = await_terminal(service, service.submit(request, bound))
        assert final.phase is RenderJobPhase.SUCCEEDED
        assert cleanup_entered.wait(5)
        # IMPORTANT: hold cleanup explicitly; waiting for an idle worker hides expired handles
        # that stay addressable only while the terminal worker still owns its cleanup reference.
        with service._condition:
            now[0] += 3
            with pytest.raises(services().RenderServiceError, match="job_unavailable"):
                service.cancel(final.job_id, final.request_fingerprint)
        with pytest.raises(services().RenderServiceError, match="job_unavailable"):
            service.status(final.job_id, final.request_fingerprint)
        with pytest.raises(services().RenderServiceError, match="job_unavailable"):
            service.artifact(final.job_id, final.request_fingerprint)
        assert service.job_count == 0
        replay = service.submit(request, bound)
        assert replay.job_id != final.job_id
        assert replay.phase is RenderJobPhase.QUEUED
    finally:
        cleanup_release.set()
        service.close()


def test_request_roundtrip_keeps_every_join_and_no_media_or_locator() -> None:
    request = _request()
    assert decode_render_job_request(request.to_wire()) == request
    plan = _plan()
    material = plan.to_wire()
    material["resolved_operations"] = [
        {
            "claimed": chunk.chunk_fingerprint,
            "observed": canonical_fingerprint(chunk.fingerprint_material()),
        }
        for chunk in plan.resolved_operations
    ]
    assert request.plan_fingerprint == canonical_fingerprint(
        {"schema": "h3.authoring.render_plan_join.v1", "plan": material}
    )
    assert request.timeline_revision == _plan().snapshot_revision
    require_request_matches_plan(request, _plan())
    assert len(request.to_wire()) == 14
    assert "source_bindings" not in request.to_wire()
    assert "resolved_operations" not in request.to_wire()


def test_job_schema_versions_and_generated_tooling_match_the_closed_wire() -> None:
    import json

    from jsonschema import Draft202012Validator

    from scripts.authoring_render_schemas import ROOT, render_wire_schemas

    request = _request().to_wire()
    schemas = render_wire_schemas()
    for name, schema in schemas.items():
        assert (
            json.loads((ROOT / "governance" / "contracts" / name).read_text(encoding="utf-8"))
            == schema
        )
        Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schemas["authoring_render_job_request_v1.schema.json"])
    validator.validate(request)
    for key in ("workspace_handle", "idempotency_key"):
        malformed = {**request, key: str(request[key]) + "\n"}
        assert not validator.is_valid(malformed)
        with pytest.raises(RenderJobError, match="invalid_request"):
            decode_render_job_request(malformed)
    for schema_version in (
        "h3.authoring.render_job_request.v0",
        "h3.authoring.render_job_request.v2",
    ):
        changed = {**request, "schema": schema_version}
        assert not validator.is_valid(changed)
        with pytest.raises(RenderJobError, match="invalid_request"):
            decode_render_job_request(changed)


@pytest.mark.parametrize("key", ["path", "url", "argv", "codec", "filter", "audio", "waveform"])
def test_unknown_private_or_audio_request_fields_fail_closed_without_echo(key: str) -> None:
    wire = _request().to_wire()
    wire[key] = "private-sentinel-never-echo"
    with pytest.raises(RenderJobError, match="invalid_request") as error:
        decode_render_job_request(wire)
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("timeout", [True, 0, -1, 900_001, 1.0, "1000"])
def test_request_deadline_is_bounded_exact_integer(timeout: object) -> None:
    wire = _request().to_wire()
    wire["timeout_ms"] = timeout
    with pytest.raises(RenderJobError, match="invalid_request"):
        decode_render_job_request(wire)


@pytest.mark.parametrize(
    "key",
    [
        "plan_fingerprint",
        "snapshot_fingerprint",
        "source_manifest_fingerprint",
        "font_facts_fingerprint",
        "font_package_fingerprint",
        "renderer_profile_fingerprint",
        "currentness_fingerprint",
        "output_profile_fingerprint",
    ],
)
def test_each_fingerprint_join_is_checked_independently(key: str) -> None:
    wire = _request().to_wire()
    wire[key] = canonical_fingerprint({"different": key})
    request = decode_render_job_request(wire)
    with pytest.raises(RenderJobError, match="plan_mismatch"):
        require_request_matches_plan(request, _plan())


def test_same_key_conflict_identity_includes_deadline_and_revision() -> None:
    request = _request()
    assert request.fingerprint != replace(request, timeout_ms=request.timeout_ms + 1).fingerprint
    assert (
        request.fingerprint
        != replace(request, timeline_revision=request.timeline_revision + 1).fingerprint
    )
    assert request.fingerprint == decode_render_job_request(request.to_wire()).fingerprint


def test_semantic_identity_excludes_revision_and_currentness_provenance() -> None:
    plan = _plan()
    edited = replace(
        plan,
        snapshot_revision=plan.snapshot_revision + 1,
        snapshot_fingerprint=canonical_fingerprint({"revision": "different"}),
        idempotency_fingerprint=canonical_fingerprint({"provenance": "different"}),
    )
    assert canonical_fingerprint(plan.to_wire()) != canonical_fingerprint(edited.to_wire())
    assert semantic_render_fingerprint(plan) == semantic_render_fingerprint(edited)
    chunk = plan.resolved_operations[0]
    frame = chunk.frames[0]
    changed_layer = replace(frame.layers[0], opacity_bp=3000)
    changed_frame = replace(frame, layers=(changed_layer, *frame.layers[1:]))
    changed_chunk = replace(chunk, frames=(changed_frame, *chunk.frames[1:]))
    changed_plan = replace(plan, resolved_operations=(changed_chunk, *plan.resolved_operations[1:]))
    assert semantic_render_fingerprint(plan) != semantic_render_fingerprint(changed_plan)


def test_success_requires_validating_phase_and_verified_receipt_binding() -> None:
    state = _state()
    with pytest.raises(RenderJobError, match="invalid_transition"):
        advance_render_job(
            state,
            expected_version=0,
            phase=RenderJobPhase.SUCCEEDED,
            receipt_fingerprint=canonical_fingerprint({"receipt": 1}),
        )
    for phase in (
        RenderJobPhase.PROBING,
        RenderJobPhase.PREPARING,
        RenderJobPhase.RENDERING,
        RenderJobPhase.ENCODING,
        RenderJobPhase.MUXING,
        RenderJobPhase.VALIDATING,
    ):
        state = advance_render_job(state, expected_version=state.version, phase=phase)
    with pytest.raises(RenderJobError, match="invalid_transition"):
        advance_render_job(state, expected_version=state.version, phase=RenderJobPhase.SUCCEEDED)
    receipt = canonical_fingerprint({"receipt": 1})
    done = advance_render_job(
        state,
        expected_version=state.version,
        phase=RenderJobPhase.SUCCEEDED,
        receipt_fingerprint=receipt,
    )
    assert done.terminal and done.progress_bp == 10_000
    assert done.receipt_fingerprint == receipt
    assert done.failure is None
    assert done.request_fingerprint == state.request_fingerprint


@pytest.mark.parametrize(
    "phase,failure",
    [
        (RenderJobPhase.CANCELLED, RenderJobFailure.CANCELLED),
        (RenderJobPhase.FAILED, RenderJobFailure.DEADLINE),
        (RenderJobPhase.FAILED, RenderJobFailure.SOURCE_REPLACED),
        (RenderJobPhase.FAILED, RenderJobFailure.PROCESS_FAILED),
    ],
)
def test_terminal_cannot_be_reopened_or_overwritten(
    phase: RenderJobPhase, failure: RenderJobFailure
) -> None:
    done = advance_render_job(_state(), expected_version=0, phase=phase, failure=failure)
    assert (
        advance_render_job(done, expected_version=done.version, phase=phase, failure=failure)
        is done
    )
    for next_phase in RenderJobPhase:
        if next_phase is phase:
            continue
        with pytest.raises(RenderJobError, match="terminal_immutable"):
            advance_render_job(done, expected_version=done.version, phase=next_phase)


def test_stale_callback_and_regressing_progress_are_rejected() -> None:
    running = advance_render_job(
        _state(), expected_version=0, phase=RenderJobPhase.RENDERING, progress_bp=4000
    )
    with pytest.raises(RenderJobError, match="version_conflict"):
        advance_render_job(running, expected_version=0, phase=RenderJobPhase.VALIDATING)
    with pytest.raises(RenderJobError, match="invalid_transition"):
        advance_render_job(running, expected_version=1, phase=RenderJobPhase.PROBING)
    with pytest.raises(RenderJobError, match="invalid_transition"):
        advance_render_job(
            running, expected_version=1, phase=RenderJobPhase.RENDERING, progress_bp=3999
        )


def test_limits_are_explicit_finite_and_cannot_be_silently_relaxed() -> None:
    limits = RenderJobLimits()
    assert limits.running_jobs == limits.child_processes == 1
    assert limits.max_jobs == 32 and limits.queue_depth == 8 and limits.workspace_queue_depth == 2
    assert limits.max_source_set_bytes == 256 * 1024 * 1024
    for field, value in (
        ("running_jobs", 2),
        ("queue_depth", 9),
        ("source_lease_seconds", 901),
        ("child_memory_bytes", 0),
        ("max_output_bytes", True),
    ):
        with pytest.raises(RenderJobError, match="invalid_limits"):
            replace(limits, **{field: value})
