"""Deterministic cross-run progress and late artifact authority regressions."""

from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

import pytest
import test_m23_15_sequence_coordinator as fixture
from test_managed_generation_publication_bridge import _PublicationBridge

from comfyui_h3_context.adapters.comfyui_production_workspace import PRODUCTION_ACTION_SCHEMA
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    RELEASE_REQUEST_V2_SCHEMA,
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
    SequenceCoordinatorResponse,
)
from comfyui_h3_context.adapters.managed_sequence_service import ManagedSequenceServiceError
from comfyui_h3_context.core.managed_run_release import ReleaseIntent
from comfyui_h3_context.core.production_workbench import ProductionWorkbenchProjection


def _runs(
    tmp_path: Path, count: int = 2
) -> tuple[SequenceCoordinatorRegistry, list[SequenceCoordinatorResponse]]:
    production, context = fixture._production_context()
    output = tmp_path / "output"
    output.mkdir()
    (output / "video.mp4").write_bytes(b"synthetic-video-payload")
    identities = iter(str(index) * 40 for index in range(1, count + 1))
    coordinator = fixture._coordinator(
        production, output, tmp_path / "private", token_factory=lambda: next(identities)
    )
    runs = []
    for index in range(count):
        projection = production.dispatch(
            {
                "schema": PRODUCTION_ACTION_SCHEMA,
                "request_id": f"create.concurrent.{index}",
                "action": "create_workspace_from_context",
                "payload": {"context_workspace_handle": context},
            }
        ).projection
        assert isinstance(projection, ProductionWorkbenchProjection)
        prepared = fixture._required_response(
            coordinator.dispatch(
                fixture._action(
                    f"prepare.concurrent.{index}",
                    "prepare_sequence",
                    {
                        "workspace_handle": projection.workspace_handle,
                        "expected_workspace_revision": projection.workspace_revision,
                        "expected_workspace_fingerprint": projection.workspace_fingerprint,
                        "correlation": {
                            "prompt_id": "prompt.bootstrap",
                            "execution_node_id": "node.product.shell",
                        },
                        "observation": fixture._observation(),
                    },
                )
            )
        )
        command = prepared.sequence.eligible_commands[0]
        runs.append(
            fixture._required_response(
                coordinator.dispatch(
                    fixture._action(
                        f"submit.concurrent.{index}",
                        "record_submission",
                        {
                            "run_handle": prepared.run_handle,
                            "expected_state_fingerprint": prepared.sequence.state.fingerprint,
                            "job_id": command.job_id,
                            "transaction_id": command.transaction_id,
                            "graph_fingerprint": command.job.graph_fingerprint,
                            "compiled_prompt_fingerprint": command.job.compiled_prompt_fingerprint,
                            "queue_prompt_id": f"prompt.concurrent.{index}",
                        },
                    )
                )
            )
        )
    return coordinator, runs


def _artifact(run: SequenceCoordinatorResponse, index: int = 0) -> dict[str, object]:
    return fixture._action(
        f"artifact.concurrent.{index}",
        "record_artifact",
        {
            "run_handle": run.run_handle,
            "expected_state_fingerprint": run.sequence.state.fingerprint,
            "queue_prompt_id": f"prompt.concurrent.{index}",
            "output_node_id": "node.save.video",
            "locator": {"filename": "video.mp4", "subfolder": "", "type": "output"},
        },
    )


def _thread(action: Callable[[], object]) -> tuple[Thread, Event, list[object]]:
    done = Event()
    results: list[object] = []

    def invoke() -> None:
        try:
            results.append(action())
        except Exception as exc:
            results.append(exc)
        finally:
            done.set()

    thread = Thread(target=invoke, daemon=True)
    thread.start()
    return thread, done, results


@pytest.mark.parametrize("action", ["read_sequence", "cancel_sequence"])
def test_unrelated_run_progresses_while_artifact_inspector_is_paused(
    tmp_path: Path, action: str
) -> None:
    coordinator, runs = _runs(tmp_path)
    entered, release = Event(), Event()

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        entered.set()
        assert release.wait(10), "test inspector was never released"
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator._artifact_inspector = inspect
    worker, finished, results = _thread(lambda: coordinator.dispatch(_artifact(runs[0])))
    other: Thread | None = None
    try:
        assert entered.wait(2)
        payload: dict[str, object] = {"run_handle": runs[1].run_handle}
        if action == "cancel_sequence":
            payload["expected_state_fingerprint"] = runs[1].sequence.state.fingerprint
        other, done, other_results = _thread(
            lambda: coordinator.dispatch(fixture._action("other.progress", action, payload))
        )
        assert done.wait(2), "unrelated request waited for the paused media inspector"
        assert not release.is_set()
        assert not isinstance(other_results[0], Exception)
    finally:
        release.set()
        worker.join(5)
        if other is not None:
            other.join(5)
    assert finished.is_set()
    assert not isinstance(results[0], Exception)


def test_cancel_during_inspection_reaches_probe_and_prevents_artifact_publication(
    tmp_path: Path,
) -> None:
    coordinator, runs = _runs(tmp_path, 1)
    entered, release = Event(), Event()
    cancellation: list[Callable[[], bool]] = []

    def inspect(
        _payload: bytes, *, should_cancel: Callable[[], bool], **_expected: object
    ) -> ObservedVideoArtifact:
        cancellation.append(should_cancel)
        entered.set()
        assert release.wait(10)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator._artifact_inspector = inspect
    worker, finished, results = _thread(lambda: coordinator.dispatch(_artifact(runs[0])))
    cancel: Thread | None = None
    try:
        assert entered.wait(2)
        cancel, done, outcomes = _thread(
            lambda: coordinator.dispatch(
                fixture._action(
                    "cancel.during.inspection",
                    "cancel_sequence",
                    {
                        "run_handle": runs[0].run_handle,
                        "expected_state_fingerprint": runs[0].sequence.state.fingerprint,
                    },
                )
            )
        )
        assert done.wait(2), "cancel waited for media verification"
        assert not isinstance(outcomes[0], Exception)
        assert cancellation[0]() is True
    finally:
        release.set()
        worker.join(5)
        if cancel is not None:
            cancel.join(5)
    assert finished.is_set()
    assert isinstance(results[0], SequenceCoordinatorError)
    assert coordinator._runs[runs[0].run_handle].artifact_receipt is None
    assert (
        coordinator._runs[runs[0].run_handle].state.runtimes[0].state.value == "unknown_ownership"
    )


def test_pending_duplicate_is_uncached_and_conflicting_digest_refuses(tmp_path: Path) -> None:
    coordinator, runs = _runs(tmp_path, 1)
    entered, release = Event(), Event()
    calls: list[int] = []

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator._artifact_inspector = inspect
    action = _artifact(runs[0])
    worker, done, results = _thread(lambda: coordinator.dispatch(action))
    try:
        assert entered.wait(2)
        pending = fixture._required_response(coordinator.dispatch(action))
        assert pending.disposition == "verification_pending"
        assert action["request_id"] not in coordinator._ledger
        conflict = dict(action, action="read_sequence", payload={"run_handle": runs[0].run_handle})
        with pytest.raises(SequenceCoordinatorError, match="request_id_conflict"):
            coordinator.dispatch(conflict)
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(5)
    assert done.is_set()
    assert not isinstance(results[0], Exception)
    assert coordinator.dispatch(action) == results[0]
    assert len(calls) == 1
    assert not coordinator._artifact_requests


@pytest.mark.parametrize("phase", ["read", "store", "preview"])
def test_heavy_artifact_phases_leave_the_coordinator_lock_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    coordinator, runs = _runs(tmp_path)
    entered, release = Event(), Event()
    if phase == "read":
        original = coordinator._resolved_artifact_payload

        def read(locator: object) -> bytes:
            entered.set()
            assert release.wait(10)
            return original(locator)  # type: ignore[arg-type]

        monkeypatch.setattr(coordinator, "_resolved_artifact_payload", read)
    elif phase == "store":
        store = coordinator._store()
        original_commit = store.commit

        def commit(*args: object, **kwargs: object) -> object:
            entered.set()
            assert release.wait(10)
            return original_commit(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store, "commit", commit)
    else:
        # CRITICAL: prepare the real store before measuring preview-phase lock availability;
        # lazy durable setup can exhaust the entry latch on mounted filesystems before preview.
        coordinator._store()
        original_preview = coordinator._production.prepare_generated_preview_sources

        def preview(*args: object, **kwargs: object) -> object:
            entered.set()
            assert release.wait(10)
            return original_preview(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(coordinator._production, "prepare_generated_preview_sources", preview)
    worker, done, results = _thread(lambda: coordinator.dispatch(_artifact(runs[0])))
    reader: Thread | None = None
    try:
        assert entered.wait(2)
        reader, read_done, outcomes = _thread(
            lambda: coordinator.dispatch(
                fixture._action(
                    "read.heavy.phase", "read_sequence", {"run_handle": runs[1].run_handle}
                )
            )
        )
        assert read_done.wait(2)
        assert not isinstance(outcomes[0], Exception)
        assert not release.is_set()
    finally:
        release.set()
        worker.join(5)
        if reader is not None:
            reader.join(5)
    assert done.is_set()
    assert not isinstance(results[0], Exception)


def test_cancel_after_private_commit_never_publishes_the_committed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, runs = _runs(tmp_path, 1)
    entered, release = Event(), Event()
    store = coordinator._store()
    original_commit = store.commit
    committed: list[object] = []

    def commit(*args: object, **kwargs: object) -> object:
        result = original_commit(*args, **kwargs)  # type: ignore[arg-type]
        committed.append(result)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(store, "commit", commit)
    worker, done, results = _thread(lambda: coordinator.dispatch(_artifact(runs[0])))
    try:
        assert entered.wait(2)
        cancelled = fixture._required_response(
            coordinator.dispatch(
                fixture._action(
                    "cancel.after.private.commit",
                    "cancel_sequence",
                    {
                        "run_handle": runs[0].run_handle,
                        "expected_state_fingerprint": runs[0].sequence.state.fingerprint,
                    },
                )
            )
        )
        assert cancelled.artifact_authority is None
        assert len(committed) == 1
    finally:
        release.set()
        worker.join(5)
    assert done.is_set()
    assert isinstance(results[0], SequenceCoordinatorError)
    assert coordinator._runs[runs[0].run_handle].artifact_receipt is None
    assert coordinator._runs[runs[0].run_handle].artifact_candidate is None
    assert not coordinator._artifact_requests


def test_two_active_verifications_bound_work_and_memory_admission(tmp_path: Path) -> None:
    coordinator, runs = _runs(tmp_path, 3)
    entered = [Event(), Event()]
    release = Event()
    calls: list[int] = []

    def inspect(_payload: bytes, **_expected: object) -> ObservedVideoArtifact:
        index = len(calls)
        calls.append(index)
        entered[index].set()
        assert release.wait(10)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator._artifact_inspector = inspect
    workers = []
    try:
        for index in range(2):

            def dispatch_artifact(index: int = index) -> object:
                return coordinator.dispatch(_artifact(runs[index], index))

            workers.append(_thread(dispatch_artifact))
            assert entered[index].wait(2)
        assert len(coordinator._artifact_requests) == 2
        with pytest.raises(SequenceCoordinatorError, match="artifact_verification_capacity"):
            coordinator.dispatch(_artifact(runs[2], 2))
        assert calls == [0, 1]
    finally:
        release.set()
        for thread, _done, _results in workers:
            thread.join(5)
    assert all(done.is_set() for _thread_, done, _results in workers)
    assert not coordinator._artifact_requests


@pytest.mark.parametrize("transition", ["detach", "expiry", "terminal_error", "managed_cancel"])
@pytest.mark.parametrize("inspector_raises", [False, True])
def test_late_verification_never_overwrites_changed_lifecycle(
    tmp_path: Path, transition: str, inspector_raises: bool
) -> None:
    coordinator, runs = _runs(tmp_path, 1)
    entered, release = Event(), Event()
    probes: list[Callable[[], bool]] = []

    def inspect(
        _payload: bytes, *, should_cancel: Callable[[], bool], **_expected: object
    ) -> ObservedVideoArtifact:
        probes.append(should_cancel)
        entered.set()
        assert release.wait(10)
        if inspector_raises:
            raise SequenceCoordinatorError("artifact_media_invalid", 422)
        return ObservedVideoArtifact("mp4", (192, 512, 512, 3))

    coordinator._artifact_inspector = inspect
    worker, done, results = _thread(lambda: coordinator.dispatch(_artifact(runs[0])))
    try:
        assert entered.wait(2)
        if transition == "detach":
            coordinator.dispatch(
                fixture._action(
                    "detach.during.verification",
                    "release_sequence",
                    {
                        "schema": RELEASE_REQUEST_V2_SCHEMA,
                        "run_handle": runs[0].run_handle,
                        "intent": ReleaseIntent.DETACH_CLIENT.value,
                        "expected_state_fingerprint": runs[0].sequence.state.fingerprint,
                    },
                )
            )
        elif transition == "terminal_error":
            coordinator.dispatch(
                fixture._action(
                    "error.during.verification",
                    "record_terminal",
                    {
                        "run_handle": runs[0].run_handle,
                        "expected_state_fingerprint": runs[0].sequence.state.fingerprint,
                        "queue_prompt_id": "prompt.concurrent.0",
                        "kind": "error",
                    },
                )
            )
        elif transition == "managed_cancel":
            coordinator._managed.cancel(runs[0].run_handle)
        else:
            coordinator._clock = lambda: 10_000.0
            coordinator._managed._clock = coordinator._clock
        assert probes[0]() is True
    finally:
        release.set()
        worker.join(5)
    assert done.is_set()
    assert isinstance(results[0], SequenceCoordinatorError)
    assert results[0].code == "stale_artifact_verification"
    entry = coordinator._runs.get(runs[0].run_handle)
    assert entry is None or entry.artifact_receipt is None
    assert not coordinator._artifact_requests


def test_parent_completion_verifies_originals_without_holding_coordinator_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    entered, release = Event(), Event()
    store = bridge.coordinator._store()
    original = store.inspect

    def inspect(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "inspect", inspect)
    worker, done, results = _thread(lambda: bridge.service.record_terminal(bridge.last_terminal))
    reader: Thread | None = None
    try:
        assert entered.wait(2)
        reader, read_done, outcomes = _thread(
            lambda: bridge.coordinator.dispatch(
                fixture._action(
                    "read.during.parent.inspection",
                    "read_sequence",
                    {"run_handle": "mc_" + "x" * 40},
                )
            )
        )
        assert read_done.wait(2), "parent store inspection held the global coordinator lock"
        assert isinstance(outcomes[0], SequenceCoordinatorError)
        assert outcomes[0].code == "run_unavailable"
    finally:
        release.set()
        worker.join(5)
        if reader is not None:
            reader.join(5)
    assert done.is_set()
    assert not isinstance(results[0], Exception)


@pytest.mark.parametrize("change", ["children", "reservation", "expiry"])
def test_parent_completion_cannot_publish_after_owner_snapshot_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    bridge = _PublicationBridge(tmp_path, monkeypatch)
    bridge.complete_child(0)
    bridge.complete_child(1, publish_terminal=False)
    entered, release = Event(), Event()
    store = bridge.coordinator._store()
    original = store.inspect

    def inspect(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "inspect", inspect)
    worker, done, results = _thread(lambda: bridge.service.record_terminal(bridge.last_terminal))
    try:
        assert entered.wait(2)
        with bridge.coordinator._lock:
            resource = next(iter(bridge.coordinator._managed_start_resources.values()))
            if change == "children":
                resource.completed_children.clear()
            elif change == "reservation":
                resource.ledger_released = True
            else:
                bridge.now[0] = resource.expires_at + 1
    finally:
        release.set()
        worker.join(5)
    assert done.is_set()
    assert isinstance(results[0], ManagedSequenceServiceError)
    assert results[0].code == "production_publication_authority"
    assert (
        bridge.production._entries[bridge.handle].accepted_authorities.generation_sequence is None
    )
    assert bridge.coordinator._artifact_slots.acquire(blocking=False)
    assert bridge.coordinator._artifact_slots.acquire(blocking=False)
    assert not bridge.coordinator._artifact_slots.acquire(blocking=False)
    bridge.coordinator._artifact_slots.release()
    bridge.coordinator._artifact_slots.release()
