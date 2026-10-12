"""Owned Windows worker shutdown completes inherited-handle disposal before cleanup."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from comfyui_h3_context.adapters.perception_process import OwnedWindowsJob


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows kernel Job")
@pytest.mark.parametrize("spawn_child", [False, True])
def test_close_completes_real_members_before_removing_inherited_files(
    tmp_path: Path, spawn_child: bool
) -> None:
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    class ProcessIds(ctypes.Structure):
        _fields_ = [
            ("assigned", wintypes.DWORD),
            ("count", wintypes.DWORD),
            ("ids", ctypes.c_size_t * 32),
        ]

    owned = tmp_path / "owned"
    owned.mkdir()
    fixture = tmp_path / "family.py"
    fixture.write_text(
        "import pathlib, subprocess, sys, time\n"
        + (
            "subprocess.Popen([sys.executable, '-I', '-c', "
            "\"import pathlib,time; pathlib.Path('child-ready').touch(); time.sleep(30)\"])\n"
            "while not pathlib.Path('child-ready').exists(): time.sleep(0.001)\n"
            if spawn_child
            else ""
        )
        + "pathlib.Path('ready').touch()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    handles = []
    process = None
    job = None
    try:
        with (owned / "output").open("wb") as output, (owned / "errors").open("wb") as errors:
            process = subprocess.Popen(  # noqa: S603 -- issued local fixture, owned directory
                [sys.executable, "-I", str(fixture)],
                cwd=owned,
                stdout=output,
                stderr=errors,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004,
            )
            job = OwnedWindowsJob(process)
            deadline = time.monotonic() + 5
            while not (owned / "ready").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.001)
            ids = ProcessIds()
            assert job.kernel.QueryInformationJobObject(
                job.handle, 3, ctypes.byref(ids), ctypes.sizeof(ids), None
            )
            assert 1 + int(spawn_child) <= ids.count <= 32
            for pid in ids.ids[: ids.count]:
                handle = kernel.OpenProcess(0x00100000, False, pid)
                assert handle
                handles.append(handle)
            assert all(kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles)
            job.close()
            states = [kernel.WaitForSingleObject(handle, 0) for handle in handles]
            assert states == [0] * len(handles), "close returned before real members completed"
            process.wait(timeout=5)
        for path in owned.iterdir():
            path.unlink()
        owned.rmdir()
        assert not owned.exists()
        job.close()  # Completed shutdown remains idempotent.
    finally:
        if job is not None:
            job.close()
        if process is not None:
            process.wait(timeout=5)
        for handle in handles:
            kernel.CloseHandle(handle)


@pytest.mark.parametrize(
    "failure", ["enumeration", "partial_open", "membership", "termination", "accounting", "wait"]
)
def test_cleanup_refuses_api_failure_without_leaking_retained_handles(failure: str) -> None:
    kernel = Mock()
    captured = []

    def query(_job: object, kind: int, buffer: Any, *_args: object) -> int:
        if kind == 3:
            if failure == "enumeration":
                return 0
            header = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
            header[0] = header[1] = 2
            ids = (ctypes.c_size_t * 2).from_address(
                ctypes.addressof(header.contents) + 2 * ctypes.sizeof(wintypes.DWORD)
            )
            ids[:] = [404, 405]
        return int(failure != "accounting" or kind != 1)

    def open_process(_access: int, _inherit: bool, pid: int) -> int:
        if failure == "partial_open" and pid == 405:
            return 0
        captured.append(pid + 1000)
        return pid + 1000

    def membership(handle: int, _job: object, result: Any) -> int:
        ctypes.cast(result, ctypes.POINTER(wintypes.BOOL))[0] = int(
            failure != "membership" or handle != 1405
        )
        return 1

    kernel.QueryInformationJobObject.side_effect = query
    kernel.OpenProcess.side_effect = open_process
    kernel.IsProcessInJob.side_effect = membership
    kernel.TerminateJobObject.return_value = int(failure != "termination")
    kernel.WaitForSingleObject.return_value = 258 if failure == "wait" else 0
    job = OwnedWindowsJob.__new__(OwnedWindowsJob)
    job.kernel, job.handle = kernel, 101
    with pytest.raises(RuntimeError, match="worker_cleanup_failed"):
        job.close()
    assert job.handle is None
    assert [call.args[0] for call in kernel.CloseHandle.call_args_list] == [*captured, 101]
    if failure in {"enumeration", "partial_open", "membership"}:
        kernel.WaitForSingleObject.assert_not_called()
    calls = list(kernel.CloseHandle.call_args_list)
    job.close()
    assert kernel.CloseHandle.call_args_list == calls


def test_cleanup_resizes_a_partial_job_list_before_terminating() -> None:
    kernel = Mock()
    calls = 0

    def query(_job: object, kind: int, buffer: Any, *_args: object) -> int:
        nonlocal calls
        if kind == 3:
            calls += 1
            header = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
            header[0] = 40 if calls == 1 else 1
            header[1] = 0 if calls == 1 else 1
            if calls == 1:
                return 0
            ids = (ctypes.c_size_t * 1).from_address(
                ctypes.addressof(header.contents) + 2 * ctypes.sizeof(wintypes.DWORD)
            )
            ids[0] = 404
        return 1

    def membership(_process: object, _job: object, result: Any) -> int:
        ctypes.cast(result, ctypes.POINTER(wintypes.BOOL))[0] = 1
        return 1

    kernel.QueryInformationJobObject.side_effect = query
    kernel.OpenProcess.return_value = 1404
    kernel.IsProcessInJob.side_effect = membership
    kernel.TerminateJobObject.return_value = 1
    kernel.WaitForSingleObject.return_value = 0
    job = OwnedWindowsJob.__new__(OwnedWindowsJob)
    job.kernel, job.handle = kernel, 101
    job.close()
    assert calls == 2
    kernel.TerminateJobObject.assert_called_once_with(101, 1)
    assert [call.args[0] for call in kernel.CloseHandle.call_args_list] == [1404, 101]
