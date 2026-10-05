"""Bounded process-local Authoring jobs; no routes, public handles or implicit runtime.

The injected backend is a trusted internal execution boundary, never browser input. It must
independently require its real runtime qualification before admission and again before execution.
Identity digests here bind that decision; constructing them does not qualify an executable.
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from ..core.authoring_render_jobs import (
    AuthoringRenderJobRequestV1,
    RenderJobError,
    RenderJobFailure,
    RenderJobLimits,
    RenderJobPhase,
    RenderJobState,
    advance_render_job,
    require_request_matches_plan,
)
from ..core.authoring_render_receipts import (
    MeasuredRenderOutput,
    RenderReceiptError,
    make_render_receipt,
    verify_render_receipt,
)
from ..core.render_planner import RenderPlanV1
from .authoring_render_leases import (
    AuthoringRenderJobSources,
    RenderSourceLeaseError,
    acquire_render_job_sources,
)
from .authoring_render_source import BoundAuthoringRenderPlan, prepare_bound_render_plan
from .authoring_render_store import (
    PrivateRenderArtifact,
    RenderOutputStore,
    RenderStage,
    RenderStoreError,
)
from .authoring_source_binding import AuthoringSourceBindingError

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MONITOR_INTERVAL = 0.05
_Result = TypeVar("_Result")


class RenderServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_request",
            "plan_mismatch",
            "idempotency_conflict",
            "job_unavailable",
            "output_unavailable",
            "runtime_unavailable",
            "resource_limit",
            "service_closed",
            "source_unavailable",
            "workspace_released",
            "deadline",
            "cleanup_failed",
        }:
            code = "invalid_request"
        self.code = code
        super().__init__(code)


class RenderExecutionError(RuntimeError):
    def __init__(self, failure: RenderJobFailure) -> None:
        self.failure = (
            failure if type(failure) is RenderJobFailure else RenderJobFailure.PROCESS_FAILED
        )
        super().__init__(self.failure.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderExecutionIdentity:
    renderer_fingerprint: str
    probe_fingerprint: str
    qualification_fingerprint: str
    profile_fingerprint: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or _DIGEST.fullmatch(value) is None
            for value in (
                self.renderer_fingerprint,
                self.probe_fingerprint,
                self.qualification_fingerprint,
                self.profile_fingerprint,
            )
        ):
            raise RenderServiceError("runtime_unavailable")


class RenderExecutionBackend(Protocol):
    def require_qualified(
        self, plan: RenderPlanV1, limits: RenderJobLimits
    ) -> RenderExecutionIdentity: ...

    def render(
        self,
        *,
        plan: RenderPlanV1,
        sources: AuthoringRenderJobSources,
        stage: RenderStage,
        control: RenderExecutionControl,
    ) -> None: ...

    def probe(self, *, path: Path, control: RenderExecutionControl) -> MeasuredRenderOutput: ...


@dataclass(slots=True, repr=False)
class _Entry:
    request: AuthoringRenderJobRequestV1
    bound: BoundAuthoringRenderPlan | None
    state: RenderJobState
    identity: RenderExecutionIdentity
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    busy: bool = False
    expires: float | None = None
    artifact: PrivateRenderArtifact | None = None


class RenderExecutionControl:
    """Private cooperative stop/progress contract, also consumed by the bounded process runner."""

    def __init__(self, service: AuthoringRenderService, entry: _Entry) -> None:
        self._service = service
        self._entry = entry
        self._sources: AuthoringRenderJobSources | None = None
        self._resources = ExitStack()
        self._resources_closed = False

    def retain_resource(self, resource: AbstractContextManager[_Result]) -> _Result:
        """Own runtime pins/job handles across encoding and both independent probes."""
        self.check()
        if self._resources_closed:
            raise RenderExecutionError(RenderJobFailure.PROCESS_FAILED)
        return self._resources.enter_context(resource)

    def _close_resources(self) -> None:
        if not self._resources_closed:
            self._resources_closed = True
            self._resources.close()

    @property
    def deadline(self) -> float:
        return self._entry.deadline

    def is_cancelled(self) -> bool:
        return (
            self._entry.cancelled.is_set()
            or self._service._stop.is_set()
            or (self._sources is not None and self._sources.is_cancelled())
        )

    def check(self) -> None:
        if self._service._stop.is_set():
            raise RenderExecutionError(RenderJobFailure.SERVICE_CLOSED)
        if self._service._now() >= self.deadline:
            raise RenderExecutionError(RenderJobFailure.DEADLINE)
        if self._entry.cancelled.is_set():
            raise RenderExecutionError(self._entry.state.failure or RenderJobFailure.CANCELLED)
        if self._sources is not None and self._sources.is_cancelled():
            self._sources.confirm_currentness()  # Preserve the distinct source-revocation reason.

    def progress(self, phase: RenderJobPhase, progress_bp: int) -> None:
        self.check()
        if phase in {
            RenderJobPhase.QUEUED,
            RenderJobPhase.SUCCEEDED,
            RenderJobPhase.FAILED,
            RenderJobPhase.CANCELLED,
        }:
            raise RenderExecutionError(RenderJobFailure.PROCESS_FAILED)
        with self._service._condition:
            self.check()
            state = self._entry.state
            if state.terminal:
                raise RenderExecutionError(state.failure or RenderJobFailure.CANCELLED)
            if (state.phase, state.progress_bp) == (phase, progress_bp):
                return
            self._entry.state = advance_render_job(
                state, expected_version=state.version, phase=phase, progress_bp=progress_bp
            )
            self._service._condition.notify_all()


class AuthoringRenderService:
    def __init__(
        self,
        store: RenderOutputStore,
        *,
        backend: RenderExecutionBackend | None = None,
    ) -> None:
        if type(store) is not RenderOutputStore:
            raise RenderServiceError("invalid_request")
        self._store = store
        self._limits = store._limits
        self._backend = backend
        self._clock = store._clock
        self._condition = threading.Condition(store._lock)
        self._entries: dict[str, _Entry] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._queue: deque[str] = deque()
        self._admissions = 0
        self._stop = threading.Event()
        self._closed = False
        self._worker = threading.Thread(target=self._work, name="h3-authoring-render", daemon=True)
        self._monitor = threading.Thread(
            target=self._watch, name="h3-authoring-render-monitor", daemon=True
        )
        self._worker.start()
        try:
            self._monitor.start()
        except BaseException:
            self._stop.set()
            with self._condition:
                self._condition.notify_all()
            self._worker.join(self._limits.cleanup_grace_seconds)
            raise RenderServiceError("cleanup_failed") from None

    def __repr__(self) -> str:
        return "<AuthoringRenderService opaque>"

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise RenderServiceError("deadline")
        return float(value)

    def _open(self) -> None:
        if self._stop.is_set():
            raise RenderServiceError("service_closed")

    @property
    def job_count(self) -> int:
        with self._condition:
            return len(self._entries)

    def _entry(self, job_id: str, request_fingerprint: str) -> _Entry:
        if type(job_id) is not str or type(request_fingerprint) is not str:
            raise RenderServiceError("job_unavailable")
        entry = self._entries.get(job_id)
        if entry is None or entry.state.request_fingerprint != request_fingerprint:
            raise RenderServiceError("job_unavailable")
        return entry

    def _replay(self, request: AuthoringRenderJobRequestV1) -> RenderJobState | None:
        existing = self._idempotency.get((request.workspace_handle, request.idempotency_key))
        if existing is None:
            return None
        entry = self._entries[existing]
        if entry.request != request:
            raise RenderServiceError("idempotency_conflict")
        return entry.state

    def _capacity(self, request: AuthoringRenderJobRequestV1) -> None:
        queued = [self._entries[job_id] for job_id in self._queue]
        if (
            len(self._entries) >= self._limits.max_jobs
            or len(queued) >= self._limits.queue_depth
            or sum(entry.request.workspace_handle == request.workspace_handle for entry in queued)
            >= self._limits.workspace_queue_depth
        ):
            raise RenderServiceError("resource_limit")

    def submit(
        self, request: AuthoringRenderJobRequestV1, bound: BoundAuthoringRenderPlan
    ) -> RenderJobState:
        if (
            type(request) is not AuthoringRenderJobRequestV1
            or type(bound) is not BoundAuthoringRenderPlan
        ):
            raise RenderServiceError("invalid_request")
        with self._condition:
            self._open()
            self._sweep(self._now())
            replay = self._replay(request)
            if replay is not None:
                return replay
            self._capacity(request)
            if self._admissions >= self._limits.queue_depth:
                raise RenderServiceError("resource_limit")
            self._admissions += 1
        try:
            deadline = self._now() + min(request.timeout_ms, self._limits.deadline_ms) / 1000.0
            if request.timeout_ms > self._limits.deadline_ms:
                raise RenderServiceError("resource_limit")
            if self._backend is None:
                raise RenderServiceError("runtime_unavailable")
            require_request_matches_plan(request, bound.plan)
            if bound._issued_snapshot is None:
                raise RenderServiceError("plan_mismatch")
            if not bound._workspace_is_current():
                raise RenderServiceError("workspace_released")
            rebuilt = prepare_bound_render_plan(
                bound._history,
                bound._issued_snapshot,
                bound._workspace_is_current,
                deadline=deadline,
            )
            if rebuilt.plan != bound.plan:
                raise RenderServiceError("plan_mismatch")
            enabled = [
                source for source in bound.plan.source_bindings if source.disposition == "enabled"
            ]
            if (
                any(
                    source.size_bytes is None
                    or source.size_bytes > self._limits.max_source_bytes
                    or (
                        source.duration_milliseconds is not None
                        and source.duration_milliseconds > self._limits.max_source_duration_ms
                    )
                    for source in enabled
                )
                or sum(source.size_bytes or 0 for source in enabled)
                > self._limits.max_source_set_bytes
            ):
                raise RenderServiceError("resource_limit")
            identity = self._backend.require_qualified(bound.plan, self._limits)
            if (
                type(identity) is not RenderExecutionIdentity
                or identity.profile_fingerprint != request.renderer_profile_fingerprint
            ):
                raise RenderServiceError("runtime_unavailable")
            with self._condition:
                self._open()
                now = self._now()
                self._sweep(now)
                if now >= deadline:
                    raise RenderServiceError("deadline")
                replay = self._replay(request)
                if replay is not None:
                    return replay
                self._capacity(request)
                job_id = "render-" + uuid.uuid4().hex
                if job_id in self._entries:
                    raise RenderServiceError("resource_limit")
                state = RenderJobState.queued(job_id, request.fingerprint)
                self._entries[job_id] = _Entry(request, bound, state, identity, deadline)
                self._idempotency[(request.workspace_handle, request.idempotency_key)] = job_id
                self._queue.append(job_id)
                self._condition.notify_all()
                return state
        except RenderJobError:
            raise RenderServiceError("plan_mismatch") from None
        except AuthoringSourceBindingError:
            raise RenderServiceError("source_unavailable") from None
        except RenderServiceError:
            raise
        except Exception:
            raise RenderServiceError("runtime_unavailable") from None
        finally:
            with self._condition:
                self._admissions -= 1

    def status(self, job_id: str, request_fingerprint: str) -> RenderJobState:
        with self._condition:
            self._open()
            self._sweep(self._now())
            return self._entry(job_id, request_fingerprint).state

    def artifact(self, job_id: str, request_fingerprint: str) -> PrivateRenderArtifact:
        with self._condition:
            self._open()
            self._sweep(self._now())
            entry = self._entry(job_id, request_fingerprint)
            if entry.state.phase is not RenderJobPhase.SUCCEEDED or entry.artifact is None:
                raise RenderServiceError("output_unavailable")
            return entry.artifact

    def cancel(self, job_id: str, request_fingerprint: str) -> RenderJobState:
        # Stop signalling cannot wait behind publication's bounded hash/currentness I/O.
        # The immutable disposition is still decided under the shared publication lock.
        entry = self._entry(job_id, request_fingerprint)
        entry.cancelled.set()
        with self._condition:
            self._open()
            # CRITICAL: signal before the publication lock, but recheck authority afterward;
            # an expired terminal handle must not return state through the cancellation path.
            self._sweep(self._now())
            entry = self._entry(job_id, request_fingerprint)
            self._fail(entry, RenderJobFailure.CANCELLED)
            return entry.state

    def _fail(self, entry: _Entry, failure: RenderJobFailure) -> None:
        if entry.state.terminal:
            return
        entry.cancelled.set()
        state = entry.state
        entry.state = advance_render_job(
            state,
            expected_version=state.version,
            phase=RenderJobPhase.CANCELLED
            if failure is RenderJobFailure.CANCELLED
            else RenderJobPhase.FAILED,
            failure=failure,
        )
        try:
            entry.expires = self._now() + self._limits.retention_seconds
        except RenderServiceError:
            # A failed clock cannot strand nonterminal work or escape the teardown path.
            self._stop.set()
        if not entry.busy:
            entry.bound = None
        self._queue = deque(job_id for job_id in self._queue if job_id != state.job_id)
        self._condition.notify_all()

    def _sweep(self, now: float) -> None:
        for job_id, entry in tuple(self._entries.items()):
            if not entry.state.terminal and now >= entry.deadline:
                self._fail(entry, RenderJobFailure.DEADLINE)
            # CRITICAL: expiry revokes status/artifact/replay authority even during terminal
            # cleanup. The worker owns its entry directly; retaining this index until it is
            # idle exposes expired handles and lets replay resurrect their old authority.
            if entry.expires is not None and now >= entry.expires:
                del self._entries[job_id]
                self._idempotency.pop(
                    (entry.request.workspace_handle, entry.request.idempotency_key), None
                )

    def _watch(self) -> None:
        while not self._stop.wait(_MONITOR_INTERVAL):
            try:
                with self._condition:
                    self._sweep(self._now())
                    pending = [
                        (self._entries[job_id], self._entries[job_id].bound)
                        for job_id in self._queue
                    ]
                    self._store.prune()
                # Workspace callbacks may acquire their own locks. Never invoke them while
                # holding the service lock: workspace teardown calls back into cancellation.
                for entry, bound in pending:
                    if bound is None:
                        continue
                    try:
                        live = bound._workspace_is_current()
                    except Exception:
                        live = False
                    if not live:
                        with self._condition:
                            if entry.state.phase is RenderJobPhase.QUEUED and entry.bound is bound:
                                self._fail(entry, RenderJobFailure.WORKSPACE_RELEASED)
            except Exception:
                self._stop.set()
                with self._condition:
                    for entry in self._entries.values():
                        self._fail(entry, RenderJobFailure.STORE_UNAVAILABLE)
                    self._condition.notify_all()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stop.is_set() or bool(self._queue))
                if self._stop.is_set():
                    return
                entry = self._entries[self._queue.popleft()]
                if entry.state.terminal:
                    continue
                entry.busy = True
                entry.state = advance_render_job(
                    entry.state, expected_version=entry.state.version, phase=RenderJobPhase.PROBING
                )
            self._execute(entry)

    @staticmethod
    def _failure(exc: BaseException) -> RenderJobFailure:
        from .authoring_render_executor import RenderPreparationError
        from .authoring_render_graph import RenderGraphError
        from .authoring_render_probe import RenderProbeError
        from .authoring_render_process import RenderProcessError

        if isinstance(exc, RenderExecutionError):
            return exc.failure
        # CRITICAL: preserve typed native cancellation/deadline/resource failures; collapsing
        # them into process_failed makes completed cleanup report the wrong terminal outcome.
        # Match owned exception types, never arbitrary third-party attributes or messages.
        if isinstance(exc, RenderProcessError):
            return {
                "cancelled": RenderJobFailure.CANCELLED,
                "deadline": RenderJobFailure.DEADLINE,
                "resource_limit": RenderJobFailure.RESOURCE_LIMIT,
                "runtime_unavailable": RenderJobFailure.RUNTIME_UNAVAILABLE,
                "session_closed": RenderJobFailure.SERVICE_CLOSED,
            }.get(exc.code, RenderJobFailure.PROCESS_FAILED)
        if isinstance(exc, RenderProbeError):
            return RenderJobFailure.OUTPUT_INVALID
        if isinstance(exc, RenderGraphError):
            return (
                RenderJobFailure.RESOURCE_LIMIT
                if exc.code == "resource_limit"
                else RenderJobFailure.PROCESS_FAILED
            )
        if isinstance(exc, RenderPreparationError):
            return {
                "font_unavailable": RenderJobFailure.FONT_CHANGED,
                "runtime_unavailable": RenderJobFailure.RUNTIME_UNAVAILABLE,
            }.get(exc.code, RenderJobFailure.SOURCE_UNAVAILABLE)
        if isinstance(exc, RenderSourceLeaseError):
            return {
                "cancelled": RenderJobFailure.CANCELLED,
                "workspace_released": RenderJobFailure.WORKSPACE_RELEASED,
                "source_expired": RenderJobFailure.SOURCE_EXPIRED,
                "source_replaced": RenderJobFailure.SOURCE_REPLACED,
                "service_closed": RenderJobFailure.SERVICE_CLOSED,
                "font_changed": RenderJobFailure.FONT_CHANGED,
                "resource_limit": RenderJobFailure.RESOURCE_LIMIT,
            }.get(exc.code, RenderJobFailure.SOURCE_UNAVAILABLE)
        if isinstance(exc, RenderReceiptError):
            return RenderJobFailure.RECEIPT_INVALID
        if isinstance(exc, RenderStoreError):
            return {
                "cancelled": RenderJobFailure.CANCELLED,
                "resource_limit": RenderJobFailure.RESOURCE_LIMIT,
                "output_invalid": RenderJobFailure.OUTPUT_INVALID,
                "receipt_invalid": RenderJobFailure.RECEIPT_INVALID,
            }.get(exc.code, RenderJobFailure.STORE_UNAVAILABLE)
        return RenderJobFailure.PROCESS_FAILED

    def _execute(self, entry: _Entry) -> None:
        sources: AuthoringRenderJobSources | None = None
        stage: RenderStage | None = None
        control = RenderExecutionControl(self, entry)
        callback_failure: RenderJobFailure | None = None

        def guarded_callback(operation: Callable[[], _Result]) -> _Result:
            nonlocal callback_failure
            try:
                return operation()
            except BaseException as exc:
                # Keep closed execution/source reasons across the store's privacy boundary;
                # its safe wrapper must not relabel source replacement as a disk failure.
                callback_failure = self._failure(exc)
                raise

        try:
            control.check()
            bound = entry.bound
            backend = self._backend
            if bound is None or backend is None:
                raise RenderExecutionError(RenderJobFailure.RUNTIME_UNAVAILABLE)
            plan = bound.plan
            if backend.require_qualified(plan, self._limits) != entry.identity:
                raise RenderExecutionError(RenderJobFailure.RUNTIME_UNAVAILABLE)
            sources = acquire_render_job_sources(
                bound,
                deadline=entry.deadline,
                limits=self._limits,
                clock=self._clock,
                cancelled=entry.cancelled,
            )
            control._sources = sources
            with self._condition:
                entry.bound = None
            del bound
            control.check()
            stage = self._store.begin(entry.state.job_id, entry.request.fingerprint)
            control.progress(RenderJobPhase.PREPARING, 500)
            backend.render(plan=plan, sources=sources, stage=stage, control=control)
            control.check()
            control.progress(RenderJobPhase.VALIDATING, max(9500, entry.state.progress_bp))
            observed = backend.probe(path=stage.output_path, control=control)
            publication = sources.confirm_currentness()
            receipt = make_render_receipt(
                request=entry.request,
                plan=plan,
                observed=observed,
                renderer_artifact_fingerprint=entry.identity.renderer_fingerprint,
                probe_artifact_fingerprint=entry.identity.probe_fingerprint,
                qualification_fingerprint=entry.identity.qualification_fingerprint,
                publication_currentness=publication,
            )

            def verify(path: Path) -> MeasuredRenderOutput:
                measured = backend.probe(path=path, control=control)
                verify_render_receipt(
                    receipt,
                    request=entry.request,
                    plan=plan,
                    observed=measured,
                    renderer_artifact_fingerprint=entry.identity.renderer_fingerprint,
                    probe_artifact_fingerprint=entry.identity.probe_fingerprint,
                    qualification_fingerprint=entry.identity.qualification_fingerprint,
                    publication_currentness=sources.confirm_currentness(),
                )
                # CRITICAL: close owned runtime resources before the publication transaction.
                # A late cleanup failure must not leave an immutable SUCCEEDED job behind;
                # keep this outside the cancellation/publication lock so stopping stays prompt.
                control._close_resources()
                control.check()
                return measured

            def publish(artifact: PrivateRenderArtifact) -> None:
                # Store invokes this under the same lock as cancellation, before commit returns.
                control.check()
                sources.confirm_currentness()
                control.check()
                state = entry.state
                if state.terminal:
                    raise RenderExecutionError(state.failure or RenderJobFailure.CANCELLED)
                final = advance_render_job(
                    state,
                    expected_version=state.version,
                    phase=RenderJobPhase.SUCCEEDED,
                    receipt_fingerprint=artifact.receipt.fingerprint,
                )
                entry.artifact = artifact
                entry.expires = artifact._expires
                entry.state = final
                self._condition.notify_all()

            self._store.commit(
                stage,
                receipt,
                verify_output=lambda path: guarded_callback(lambda: verify(path)),
                on_publish=lambda artifact: guarded_callback(lambda: publish(artifact)),
            )
        except BaseException as exc:
            with self._condition:
                try:
                    expired = self._now() >= entry.deadline
                except RenderServiceError:
                    expired = True
                failure = (
                    RenderJobFailure.DEADLINE if expired else callback_failure or self._failure(exc)
                )
                if entry.cancelled.is_set() and not entry.state.terminal and not expired:
                    failure = RenderJobFailure.CANCELLED
                self._fail(entry, failure)
        finally:
            try:
                try:
                    control._close_resources()
                finally:
                    try:
                        if sources is not None:
                            sources.release()
                        control._sources = None
                    finally:
                        if stage is not None:
                            self._store.discard(stage)
            except Exception:
                self._stop.set()
            finally:
                with self._condition:
                    entry.bound = None
                    entry.busy = False
                    self._condition.notify_all()

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            if self._closed:
                return
            for entry in self._entries.values():
                self._fail(entry, RenderJobFailure.SERVICE_CLOSED)
            self._condition.notify_all()
        deadline = time.monotonic() + self._limits.cleanup_grace_seconds
        for worker in (self._worker, self._monitor):
            if worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        if self._worker.is_alive() or self._monitor.is_alive():
            raise RenderServiceError("cleanup_failed")
        try:
            self._store.close()
        except Exception:
            raise RenderServiceError("cleanup_failed") from None
        with self._condition:
            self._entries.clear()
            self._idempotency.clear()
            self._queue.clear()
            self._closed = True
