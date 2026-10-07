"""Native owned-process resource tests, not FFmpeg composition qualification."""

from __future__ import annotations

import copy
import hashlib
import importlib
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from comfyui_h3_context.core.authoring_render_jobs import RenderJobLimits

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only render enforcement")


class Control:
    def __init__(self, seconds: float = 10.0) -> None:
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()


def processes() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_process")


def command_path() -> Path:
    return Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"


def command_pin(control: Control) -> Any:
    path = command_path()
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return processes().pin_render_executable(path, digest, control=control)


def test_owned_process_reads_back_real_job_limits_and_closes_handles(tmp_path: Path) -> None:
    control = Control()
    with command_pin(control) as executable:
        with processes().WindowsRenderProcessSession() as session:
            result = session.run(
                executable, ("/d", "/c", "echo synthetic"), cwd=tmp_path, control=control
            )
            assert result.exit_code == 0
            assert result.stdout.strip() == b"synthetic"
            assert result.active_processes == 0
            assert 0 < result.peak_committed_bytes <= RenderJobLimits().child_memory_bytes
            assert result.limits_verified
            assert str(command_path()) not in repr(result)


def test_opt_in_observer_binds_working_set_to_the_actual_job_process(tmp_path: Path) -> None:
    control = Control()
    observations: list[Any] = []
    with command_pin(control) as executable:
        with processes().WindowsRenderProcessSession(observer=observations.append) as session:
            result = session.run(
                executable, ("/d", "/c", "echo observed"), cwd=tmp_path, control=control
            )
    assert result.exit_code == 0
    assert len(observations) == 1
    observation = observations[0]
    assert observation.process_id > 0
    assert observation.job_membership_verified
    assert observation.working_set_valid
    assert observation.working_set_failure is None
    assert observation.working_set_sample_count > 0
    assert observation.peak_working_set_bytes > 0
    assert observation.peak_committed_bytes == result.peak_committed_bytes
    assert observation.limits_verified
    assert observation.executable_fingerprint == executable.fingerprint


def test_closed_multi_input_argument_vector_is_not_limited_to_255_entries(tmp_path: Path) -> None:
    control = Control()
    with command_pin(control) as executable, processes().WindowsRenderProcessSession() as session:
        result = session.run(
            executable,
            ("/d", "/c", "echo", *("x" for _ in range(300))),
            cwd=tmp_path,
            control=control,
        )
        assert result.exit_code == 0 and result.stdout.split() == [b"x"] * 300
        assert session.active_processes == 0
        with pytest.raises(processes().RenderProcessError, match="invalid_request"):
            session.run(executable, ("x",) * 4097, cwd=tmp_path, control=control)


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_owned_process_cancellation_and_deadline_reap_before_return(
    tmp_path: Path, reason: str
) -> None:
    control = Control(0.2 if reason == "deadline" else 10.0)
    timer = threading.Timer(0.15, control.cancelled.set)
    with command_pin(control) as executable:
        with processes().WindowsRenderProcessSession() as session:
            if reason == "cancelled":
                timer.start()
            try:
                with pytest.raises(processes().RenderProcessError, match=reason):
                    session.run(
                        executable,
                        ("/d", "/c", "for /L %n in (0,0,1) do @rem"),
                        cwd=tmp_path,
                        control=control,
                    )
                assert session.active_processes == 0
            finally:
                timer.cancel()
                if reason == "cancelled":
                    timer.join(1)


def test_owned_process_capture_budget_terminates_noisy_child(tmp_path: Path) -> None:
    control = Control()
    with command_pin(control) as executable:
        with processes().WindowsRenderProcessSession() as session:
            with pytest.raises(processes().RenderProcessError, match="resource_limit"):
                session.run(
                    executable,
                    ("/d", "/c", "for /L %n in (1,1,999999) do @echo synthetic"),
                    cwd=tmp_path,
                    control=control,
                    maximum_stdout=64,
                )
            assert session.active_processes == 0


def test_owned_job_denies_a_second_process_instead_of_leaking_a_child(tmp_path: Path) -> None:
    control = Control()
    with command_pin(control) as executable:
        with processes().WindowsRenderProcessSession() as session:
            result = session.run(
                executable,
                ("/d", "/c", "cmd /d /c echo forbidden-child-output"),
                cwd=tmp_path,
                control=control,
            )
            assert result.exit_code != 0
            assert b"forbidden-child-output" not in result.stdout
            assert result.active_processes == 0


def test_wrong_executable_digest_is_rejected_without_process_creation() -> None:
    with pytest.raises(processes().RenderProcessError, match="runtime_unavailable"):
        processes().pin_render_executable(command_path(), "sha256:" + "0" * 64, control=Control())


def test_process_session_cannot_be_copied() -> None:
    with processes().WindowsRenderProcessSession() as session:
        with pytest.raises(TypeError):
            copy.copy(session)


def test_pin_close_during_running_child_defers_os_handle_release(tmp_path: Path) -> None:
    control = Control()
    executable = command_pin(control)
    releases: list[bool] = []
    executable._scope.callback(lambda: releases.append(True))

    def while_running() -> None:
        executable.close()
        assert not releases

    try:
        with processes().WindowsRenderProcessSession() as session:
            session.run(
                executable,
                ("/d", "/c", "echo pin"),
                cwd=tmp_path,
                control=control,
                check_staging=while_running,
            )
        assert releases == [True]
    finally:
        executable.close()


def test_process_reports_error_byte_count_without_exposing_diagnostics(tmp_path: Path) -> None:
    control = Control()
    with command_pin(control) as executable, processes().WindowsRenderProcessSession() as session:
        result = session.run(
            executable, ("/d", "/c", "echo synthetic-error 1>&2"), cwd=tmp_path, control=control
        )
        assert result.exit_code == 0
        assert result.diagnostic_bytes > 0
        assert b"synthetic-error" not in result.stdout
        assert "synthetic-error" not in repr(result)


def test_process_environment_excludes_parent_synthetic_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("H3_SYNTHETIC_PARENT", "synthetic-marker")
    control = Control()
    with command_pin(control) as executable, processes().WindowsRenderProcessSession() as session:
        result = session.run(
            executable,
            ("/d", "/c", "if defined H3_SYNTHETIC_PARENT (echo leaked) else (echo excluded)"),
            cwd=tmp_path,
            control=control,
        )
        assert result.stdout.strip() == b"excluded"


def test_process_enforces_actual_cumulative_cpu_budget(tmp_path: Path) -> None:
    control = Control()
    limits = replace(RenderJobLimits(), child_cpu_seconds=1)
    with (
        command_pin(control) as executable,
        processes().WindowsRenderProcessSession(limits=limits) as session,
    ):
        with pytest.raises(processes().RenderProcessError, match="resource_limit"):
            session.run(
                executable,
                ("/d", "/c", "for /L %n in (0,0,1) do @rem"),
                cwd=tmp_path,
                control=control,
            )
        assert session.active_processes == 0
