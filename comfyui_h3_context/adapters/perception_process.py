"""Contain the Windows venv launcher and interpreter in one owned kernel job."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from importlib import import_module
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int64),
        ("kernel", ctypes.c_int64),
        ("period_user", ctypes.c_int64),
        ("period_kernel", ctypes.c_int64),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


class _ProcessIds(ctypes.Structure):
    _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD)]


class OwnedWindowsJob:
    """Assign before resuming: process enumeration alone cannot close the spawn race."""

    def __init__(self, process: Any) -> None:
        psutil = import_module("psutil")

        # IMPORTANT: keep the real Windows loader lazy; POSIX ctypes stubs omit WinDLL.
        win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
        kernel = win_dll("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": (
                [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError("worker_containment_failed")
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise RuntimeError("worker_containment_failed")
            if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise RuntimeError("worker_containment_failed")
            threads = psutil.Process(process.pid).threads()
            if len(threads) != 1:
                raise RuntimeError("worker_containment_failed")
            thread = kernel.OpenThread(0x0002, False, threads[0].id)
            if not thread:
                raise RuntimeError("worker_containment_failed")
            try:
                if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise RuntimeError("worker_containment_failed")
            finally:
                kernel.CloseHandle(thread)
        except Exception:
            self.close()
            raise

    def _capture_members(self, handles: list[Any], deadline: float) -> None:
        capacity = 16
        while True:
            buffer = ctypes.create_string_buffer(
                ctypes.sizeof(_ProcessIds) + ctypes.sizeof(ctypes.c_size_t) * capacity
            )
            header = _ProcessIds.from_buffer(buffer)
            result = self.kernel.QueryInformationJobObject(
                self.handle, 3, buffer, ctypes.sizeof(buffer), None
            )
            if header.assigned > capacity:
                if time.monotonic() >= deadline:
                    raise RuntimeError("worker_cleanup_failed")
                capacity = int(header.assigned)
                continue
            if not result or header.count > capacity:
                raise RuntimeError("worker_cleanup_failed")
            ids = (ctypes.c_size_t * header.count).from_buffer(buffer, ctypes.sizeof(_ProcessIds))
            for pid in ids:
                handle = self.kernel.OpenProcess(0x00101000, False, pid)
                if not handle:
                    raise RuntimeError("worker_cleanup_failed")
                handles.append(handle)
                member = wintypes.BOOL()
                # CRITICAL: validate the retained handle, not a PID that may have been reused.
                if not self.kernel.IsProcessInJob(
                    handle, self.handle, ctypes.byref(member)
                ) or not (member.value):
                    raise RuntimeError("worker_cleanup_failed")
            return

    def close(self) -> None:
        if not self.handle:
            return
        handles: list[Any] = []
        deadline = time.monotonic() + 5
        try:
            # CRITICAL: accounting zero precedes real process signalling on Windows. Retain
            # member handles before termination; otherwise inherited files can outlive cleanup.
            self._capture_members(handles, deadline)
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise RuntimeError("worker_cleanup_failed")
            while True:
                accounting = _Accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ):
                    raise RuntimeError("worker_cleanup_failed")
                if accounting.active == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("worker_cleanup_failed")
                time.sleep(0.01)
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if self.kernel.WaitForSingleObject(handle, remaining) != 0:
                    raise RuntimeError("worker_cleanup_failed")
        finally:
            for handle in handles:
                self.kernel.CloseHandle(handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
