"""Production worker lifetime and causal ports, observed through real admitted jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import cast

import pytest
from test_production_import import (
    _m26_assemble_action,
    _m26_cancel_or_retry_action,
    _m26_read,
    _m26_store_backed_publication,
)

from comfyui_h3_context.adapters import comfyui_production_workspace as subject
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_store import AVStoreError
from comfyui_h3_context.adapters.m26_production_assembly import (
    M26ProductionAssemblyError,
    M26ProductionAssemblyExecution,
)
from comfyui_h3_context.adapters.media_preview_authority import MediaPreviewSourceAuthority
from comfyui_h3_context.adapters.media_runtime_manager import MediaRuntimeBusy
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


class _ObservedLock:
    def __init__(self) -> None:
        self.lock = RLock()
        self.depth = 0

    def __enter__(self) -> _ObservedLock:
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_args: object) -> None:
        self.depth -= 1
        self.lock.release()


def test_default_scheduler_is_lazy_single_worker_and_submits_outside_business_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _ObservedLock()
    allocations: list[dict[str, object]] = []
    callbacks: list[Callable[[], None]] = []

    class Executor:
        def __init__(self, **kwargs: object) -> None:
            assert lock.depth > 0
            allocations.append(kwargs)

        def submit(self, callback: Callable[[], None]) -> None:
            assert lock.depth == 0
            callbacks.append(callback)

    monkeypatch.setattr(subject, "ThreadPoolExecutor", Executor)
    registry, ready, _injected, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    registry._lock = lock  # type: ignore[assignment]
    registry._assembly_submitter = None
    assert allocations == []
    action = _m26_assemble_action(ready, "request.lifecycle.default")
    planned = registry.dispatch(action).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    registry.dispatch(action)
    assert len(callbacks) == 1
    assert allocations == [{"max_workers": 1, "thread_name_prefix": "h3-m26-assembly"}]
    registry.dispatch(
        _m26_cancel_or_retry_action(planned, "request.lifecycle.cancel", "cancel_assembly")
    )
    callbacks.pop()()
    failed = _m26_read(registry, ready)
    registry.dispatch(
        _m26_cancel_or_retry_action(failed, "request.lifecycle.retry", "retry_assembly")
    )
    assert len(callbacks) == 1 and len(allocations) == 1
    callbacks.pop()()
    assert _m26_read(registry, ready).assembly.state == "succeeded"


def test_injected_callback_retains_original_component_after_reset_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    lock = _ObservedLock()
    registry._lock = lock  # type: ignore[assignment]

    def submit(callback: Callable[[], None]) -> None:
        assert lock.depth == 0
        callbacks.append(callback)

    def forbidden(**_kwargs: object) -> None:
        pytest.fail("injected scheduling allocated a default executor")

    registry._assembly_submitter = submit
    monkeypatch.setattr(subject, "ThreadPoolExecutor", forbidden)
    replacement = object()
    with composition_root.substituted(composition_root.PRODUCTION_WORKSPACE, registry):
        action = _m26_assemble_action(ready, "request.lifecycle.retained")
        registry.dispatch(action)
        registry.dispatch(action)
        assert len(callbacks) == 1
        composition_root.reset(composition_root.PRODUCTION_WORKSPACE)
        composition_root.install(composition_root.PRODUCTION_WORKSPACE, replacement)
        callbacks.pop()()
        assert composition_root.get(composition_root.PRODUCTION_WORKSPACE) is replacement
        assert _m26_read(registry, ready).assembly.state == "succeeded"


@pytest.mark.parametrize(
    "outcome", ["success", "cancel", "stale", "execution_failure", "preview_failure"]
)
def test_entered_operation_lease_closes_once_on_each_worker_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    old_authorities, old_previews = entry.accepted_authorities, entry.preview_sources
    lock = _ObservedLock()
    registry._lock = lock  # type: ignore[assignment]
    events: list[str] = []

    @contextmanager
    def lease() -> Iterator[None]:
        assert lock.depth == 0
        events.append("enter")
        try:
            yield None
        finally:
            assert lock.depth == 0
            events.append("exit")

    registry._media_lease = lease
    planned = registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.lease")).projection
    assert isinstance(planned, ProductionWorkbenchProjection)
    if outcome == "cancel":
        registry.dispatch(
            _m26_cancel_or_retry_action(planned, "request.lifecycle.cancel", "cancel_assembly")
        )
    elif outcome == "stale":
        entry.assembly_job = None
    elif outcome == "execution_failure":

        def execution_failure(**_kwargs: object) -> M26ProductionAssemblyExecution:
            raise M26ProductionAssemblyError("controlled_execution_failure")

        monkeypatch.setattr(subject, "execute_m26_production_assembly", execution_failure)
    elif outcome == "preview_failure":

        def preview_failure(**_kwargs: object) -> None:
            raise ValueError("controlled preview refusal")

        monkeypatch.setattr(subject, "build_media_preview_source_authority", preview_failure)
    callbacks.pop()()
    assert events == ["enter", "exit"]
    if outcome != "success":
        assert entry.accepted_authorities is old_authorities
        assert entry.preview_sources is old_previews
        assert _m26_read(registry, ready).outputs == ready.outputs
    else:
        assert _m26_read(registry, ready).assembly.state == "succeeded"


@pytest.mark.parametrize("error", ["busy", "other"])
def test_failed_lease_acquisition_preserves_narrow_error_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    events: list[str] = []

    @contextmanager
    def lease() -> Iterator[None]:
        events.append("attempt")
        if error == "busy":
            raise MediaRuntimeBusy()
        raise ValueError("controlled acquisition failure")
        yield  # pragma: no cover

    registry._media_lease = lease
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.acquire"))
    callback = callbacks.pop()
    if error == "busy":
        callback()
        assert _m26_read(registry, ready).assembly.failure_code == "assembly_worker_unavailable"
    else:
        with pytest.raises(ValueError, match="controlled acquisition failure"):
            callback()
        assert _m26_read(registry, ready).assembly.state == "planned"
    assert events == ["attempt"]


def test_retirement_snapshot_and_issued_execution_objects_keep_causal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    lock = _ObservedLock()
    registry._lock = lock  # type: ignore[assignment]
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.identity"))
    job = registry._entries[ready.workspace_handle].assembly_job
    assert job is not None
    events: list[str] = []
    original_retire = job.runtime.reconstruction_store.retire_unreferenced
    original_execute = cast(
        Callable[..., M26ProductionAssemblyExecution],
        vars(subject)["execute_m26_production_assembly"],
    )
    original_snapshot = registry._live_reconstruction_transaction_ids
    original_clock = registry._clock_ms
    snapshot: dict[str, object] = {}
    expected: dict[str, object] = {}

    def capture() -> frozenset[str]:
        assert lock.depth > 0 and job.state == "running"
        result = original_snapshot()
        expected["keep_transaction_ids"] = result
        return result

    def clock() -> int:
        result = original_clock()
        if lock.depth > 0 and job.state == "running" and not events:
            expected["completed_before_ms"] = result
        return result

    def retire(**kwargs: object) -> object:
        assert lock.depth == 0 and job.state == "running"
        events.append("retire")
        snapshot.update(kwargs)
        return original_retire(**kwargs)  # type: ignore[arg-type]

    def execute(**kwargs: object) -> M26ProductionAssemblyExecution:
        assert lock.depth == 0 and events == ["retire"]
        assert kwargs["authorization"] is job.authorization
        assert kwargs["automatic_plan"] is job.automatic_plan
        assert kwargs["generation_sequence"] is job.generation_sequence
        assert kwargs["artifact_receipts"] is job.artifact_receipts
        assert kwargs["cancellation"] is job.cancellation
        events.append("execute")
        return original_execute(**kwargs)

    monkeypatch.setattr(job.runtime.reconstruction_store, "retire_unreferenced", retire)
    monkeypatch.setattr(subject, "execute_m26_production_assembly", execute)
    monkeypatch.setattr(registry, "_live_reconstruction_transaction_ids", capture)
    monkeypatch.setattr(registry, "_clock_ms", clock)
    callbacks.pop()()
    assert events == ["retire", "execute"]
    assert snapshot == expected
    assert snapshot["keep_transaction_ids"] == frozenset(
        {"m26tx_" + job.job_id.removeprefix("m26job_")}
    )
    assert isinstance(snapshot["completed_before_ms"], int)
    assert _m26_read(registry, ready).assembly.state == "succeeded"


@pytest.mark.parametrize("typed", [True, False])
def test_retirement_catches_only_the_existing_typed_store_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: bool
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.retirement"))
    job = registry._entries[ready.workspace_handle].assembly_job
    assert job is not None

    def retire(**_kwargs: object) -> None:
        if typed:
            raise AVStoreError("controlled_store_refusal")
        raise ValueError("controlled retirement failure")

    monkeypatch.setattr(job.runtime.reconstruction_store, "retire_unreferenced", retire)
    callback = callbacks.pop()
    if typed:
        callback()
        assert _m26_read(registry, ready).assembly.state == "succeeded"
    else:
        with pytest.raises(ValueError, match="controlled retirement failure"):
            callback()
        current = _m26_read(registry, ready)
        assert current.assembly.state == "running"
        assert current.assembly.failure_code is None
        assert current.outputs == ready.outputs


def test_operation_busy_is_not_reclassified_as_lease_acquisition_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    events: list[str] = []

    @contextmanager
    def lease() -> Iterator[None]:
        events.append("enter")
        try:
            yield None
        finally:
            events.append("exit")

    def operation(*_args: object) -> None:
        raise MediaRuntimeBusy()

    registry._media_lease = lease
    monkeypatch.setattr(registry, "_run_leased_assembly_job", operation)
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.operation"))
    with pytest.raises(MediaRuntimeBusy):
        callbacks.pop()()
    assert events == ["enter", "exit"]
    assert _m26_read(registry, ready).assembly.state == "planned"


def test_publication_captures_authority_and_preview_identity_after_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    original_execute = cast(
        Callable[..., M26ProductionAssemblyExecution],
        vars(subject)["execute_m26_production_assembly"],
    )
    original_factory = cast(
        Callable[..., MediaPreviewSourceAuthority],
        vars(subject)["build_media_preview_source_authority"],
    )
    observed: list[str] = []
    after: dict[str, object] = {}

    def execute(**kwargs: object) -> M26ProductionAssemblyExecution:
        result = original_execute(**kwargs)
        # Equal values with fresh identities distinguish the original post-execution snapshot.
        entry.accepted_authorities = replace(entry.accepted_authorities)
        entry.preview_sources = MappingProxyType(dict(entry.preview_sources))
        after["authorities"] = entry.accepted_authorities
        after["previews"] = entry.preview_sources
        observed.append("execute")
        return result

    def prepare(**kwargs: object) -> object:
        assert observed == ["execute"]
        assert entry.accepted_authorities is after["authorities"]
        assert entry.preview_sources is after["previews"]
        observed.append("preview")
        return original_factory(**kwargs)

    monkeypatch.setattr(subject, "execute_m26_production_assembly", execute)
    monkeypatch.setattr(subject, "build_media_preview_source_authority", prepare)
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.snapshot"))
    callbacks.pop()()
    assert observed == ["execute", "preview"]
    assert _m26_read(registry, ready).assembly.state == "succeeded"


@pytest.mark.parametrize("failure", ["capacity", "busy", "stale_preview"])
def test_registry_refuses_publication_without_replacing_usable_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    registry, ready, callbacks, _receipts = _m26_store_backed_publication(tmp_path, monkeypatch)
    entry = registry._entries[ready.workspace_handle]
    original_authorities = entry.accepted_authorities
    original_previews = entry.preview_sources
    registry.dispatch(_m26_assemble_action(ready, "request.lifecycle.refusal"))
    if failure == "capacity":
        monkeypatch.setattr(
            subject,
            "_retained_authority_bytes",
            lambda *_args: subject.MAX_PRODUCTION_AUTHORITY_BYTES + 1,
        )
    elif failure == "stale_preview":
        original_factory = cast(
            Callable[..., MediaPreviewSourceAuthority],
            vars(subject)["build_media_preview_source_authority"],
        )

        def prepare(**kwargs: object) -> object:
            result = original_factory(**kwargs)
            entry.preview_sources = MappingProxyType(dict(entry.preview_sources))
            return result

        monkeypatch.setattr(subject, "build_media_preview_source_authority", prepare)
    else:
        assert entry.mutation_claim.acquire(blocking=False)
    try:
        callbacks.pop()()
    finally:
        if failure == "busy":
            entry.mutation_claim.release()
    current = _m26_read(registry, ready)
    assert current.assembly.state == "failed"
    assert (
        current.assembly.failure_code
        == {
            "capacity": "workspace_capacity",
            "busy": "assembly_publication_busy",
            "stale_preview": "assembly_publication_stale",
        }[failure]
    )
    assert current.outputs == ready.outputs
    assert current.assembly.receipt_fingerprint is None
    assert entry.accepted_authorities is original_authorities
    if failure != "stale_preview":
        assert entry.preview_sources is original_previews
    else:
        assert dict(entry.preview_sources) == dict(original_previews)
