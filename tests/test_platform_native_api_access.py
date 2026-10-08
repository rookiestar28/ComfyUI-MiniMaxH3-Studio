"""Platform typing repairs retain native loader and fail-closed construction behavior."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from comfyui_h3_context.adapters import authoring_output_preview as preview
from comfyui_h3_context.adapters import perception_process as containment
from comfyui_h3_context.core.authoring_output_protocol import OutputProtocolError


def test_non_windows_preview_refuses_before_allocating_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = Mock(side_effect=AssertionError("store must remain unallocated"))
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        patch.setattr(preview, "RenderOutputStore", store)
        with pytest.raises(OutputProtocolError, match="preview_unavailable"):
            preview.NativeOutputPreview(
                root=tmp_path, renderer_path=tmp_path / "renderer", probe_path=tmp_path / "probe"
            )
    store.assert_not_called()


def test_owned_windows_job_uses_real_loader_contract_and_assigns_before_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def assign(*_args: object) -> int:
        events.append("assign")
        return 1

    def resume(*_args: object) -> int:
        events.append("resume")
        return 0

    kernel = Mock()
    kernel.CreateJobObjectW.return_value = 101
    kernel.SetInformationJobObject.return_value = 1
    kernel.AssignProcessToJobObject.side_effect = assign
    kernel.OpenThread.return_value = 202
    kernel.ResumeThread.side_effect = resume
    kernel.TerminateJobObject.return_value = 1
    kernel.QueryInformationJobObject.return_value = 1
    loader = Mock(return_value=kernel)
    psutil = Mock()
    psutil.Process.return_value.threads.return_value = [SimpleNamespace(id=303)]
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(containment, "import_module", lambda name: psutil)

    job = containment.OwnedWindowsJob(SimpleNamespace(pid=404, _handle=505))
    try:
        loader.assert_called_once_with("kernel32", use_last_error=True)
        kernel.AssignProcessToJobObject.assert_called_once_with(101, 505)
        kernel.OpenThread.assert_called_once_with(0x0002, False, 303)
        assert events == ["assign", "resume"]
        assert kernel.CloseHandle.call_args_list[0].args == (202,)
    finally:
        job.close()
    kernel.TerminateJobObject.assert_called_once_with(101, 1)
    assert kernel.CloseHandle.call_args_list[-1].args == (101,)
