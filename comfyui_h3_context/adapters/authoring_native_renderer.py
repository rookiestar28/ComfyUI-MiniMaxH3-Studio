"""Explicit, qualified Windows renderer for the internal Authoring service; no discovery."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.authoring_render_jobs import RenderJobLimits, RenderJobPhase
from ..core.authoring_render_receipts import MeasuredRenderOutput
from ..core.render_planner import RenderPlanV1
from .authoring_render_executor import prepare_render_assets, run_prepared_render
from .authoring_render_leases import AuthoringRenderJobSources
from .authoring_render_probe import measure_render_output
from .authoring_render_process import (
    PinnedRenderExecutable,
    RenderProcessObservation,
    WindowsRenderProcessSession,
    pin_render_executable,
)
from .authoring_render_service import (
    RenderExecutionControl,
    RenderExecutionIdentity,
    RenderServiceError,
)
from .authoring_render_store import RenderStage
from .authoring_renderer_qualification import load_renderer_qualification


@dataclass(frozen=True, slots=True)
class _AdmissionControl:
    deadline: float

    def is_cancelled(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _Runtime:
    renderer: PinnedRenderExecutable
    probe: PinnedRenderExecutable
    session: WindowsRenderProcessSession
    stage: RenderStage


class NativeAuthoringRenderer:
    """Accept explicit private tool paths only; missing qualification never launches a child."""

    def __init__(
        self,
        *,
        renderer_path: Path,
        probe_path: Path,
        process_observer: Callable[[RenderProcessObservation], None] | None = None,
    ) -> None:
        if not isinstance(renderer_path, Path) or not isinstance(probe_path, Path):
            raise RenderServiceError("runtime_unavailable")
        self._renderer_path = renderer_path
        self._probe_path = probe_path
        self._process_observer = process_observer
        self._lock = threading.RLock()
        self._runtimes: dict[RenderExecutionControl, _Runtime] = {}

    def require_qualified(
        self, plan: RenderPlanV1, limits: RenderJobLimits
    ) -> RenderExecutionIdentity:
        identity = load_renderer_qualification()
        if (
            sys.platform != "win32"
            or type(plan) is not RenderPlanV1
            or type(limits) is not RenderJobLimits
            or limits != RenderJobLimits()
            or plan.renderer_capability_profile_fingerprint != identity.profile_fingerprint
        ):
            raise RenderServiceError("runtime_unavailable")
        control = _AdmissionControl(time.monotonic() + 30.0)
        with (
            pin_render_executable(
                self._renderer_path, identity.renderer_fingerprint, control=control
            ),
            pin_render_executable(self._probe_path, identity.probe_fingerprint, control=control),
        ):
            return identity

    @contextmanager
    def _runtime(
        self, identity: RenderExecutionIdentity, stage: RenderStage, control: RenderExecutionControl
    ) -> Iterator[_Runtime]:
        with self._lock:
            if self._runtimes:
                raise RenderServiceError("resource_limit")
            with ExitStack() as scope:
                renderer = scope.enter_context(
                    pin_render_executable(
                        self._renderer_path, identity.renderer_fingerprint, control=control
                    )
                )
                probe = scope.enter_context(
                    pin_render_executable(
                        self._probe_path, identity.probe_fingerprint, control=control
                    )
                )
                session = scope.enter_context(
                    WindowsRenderProcessSession(
                        limits=stage._store._limits, observer=self._process_observer
                    )
                )
                runtime = _Runtime(renderer, probe, session, stage)
                self._runtimes[control] = runtime
                try:
                    yield runtime
                finally:
                    self._runtimes.pop(control, None)

    def render(
        self,
        *,
        plan: RenderPlanV1,
        sources: AuthoringRenderJobSources,
        stage: RenderStage,
        control: RenderExecutionControl,
    ) -> None:
        if type(control) is not RenderExecutionControl or type(stage) is not RenderStage:
            raise RenderServiceError("runtime_unavailable")
        identity = self.require_qualified(plan, stage._store._limits)
        # CRITICAL: retain the same executable pins and cumulative process job through both
        # publication probes; closing after encode resets budgets and permits runtime replacement.
        runtime = control.retain_resource(self._runtime(identity, stage, control))
        prepared = prepare_render_assets(plan=plan, sources=sources, stage=stage, control=control)
        control.progress(RenderJobPhase.RENDERING, 1000)
        run_prepared_render(
            plan=plan,
            prepared=prepared,
            renderer=runtime.renderer,
            session=runtime.session,
            control=control,
        )
        control.progress(RenderJobPhase.MUXING, 9000)

    def probe(self, *, path: Path, control: RenderExecutionControl) -> MeasuredRenderOutput:
        with self._lock:
            runtime = self._runtimes.get(control)
            if runtime is None or path != runtime.stage.output_path:
                raise RenderServiceError("runtime_unavailable")
            return measure_render_output(
                path=path,
                probe=runtime.probe,
                session=runtime.session,
                control=control,
                limits=runtime.stage._store._limits,
                check_staging=runtime.stage.check_budget,
            )
