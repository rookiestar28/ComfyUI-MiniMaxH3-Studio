"""Windows-only, explicitly pinned private process sessions with creation-time job limits.

This internal seam accepts trusted generated arguments. It is not a public command runner,
renderer qualification, filesystem sandbox, network policy, or source admission authority.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol, SupportsIndex

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.safe_paths import validate_directory, validate_regular_file
from .segment_artifact_store import (
    _msvcrt_get_osfhandle,
    _msvcrt_open_osfhandle,
    _validated_directories,
    _windows_dll,
)

_DEFAULT_LIMITS = RenderJobLimits()
_PIN_TOKEN = object()
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_LIMIT_FLAGS = 0x0002 | 0x0004 | 0x0008 | 0x0100 | 0x0200 | 0x0400 | 0x2000
_TICKS_PER_SECOND = 10_000_000


class RenderProcessError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {
            "runtime_unavailable",
            "invalid_request",
            "cancelled",
            "deadline",
            "resource_limit",
            "process_failed",
            "cleanup_failed",
            "session_closed",
        }:
            code = "process_failed"
        self.code = code
        super().__init__(code)


class ProcessControl(Protocol):
    @property
    def deadline(self) -> float: ...

    def is_cancelled(self) -> bool: ...


def _guard(control: ProcessControl) -> None:
    if control.is_cancelled():
        raise RenderProcessError("cancelled")
    if type(control.deadline) not in {float, int} or not math.isfinite(control.deadline):
        raise RenderProcessError("deadline")
    if time.monotonic() >= control.deadline:
        raise RenderProcessError("deadline")


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _Startup), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


def _kernel() -> Any:
    if sys.platform != "win32":
        raise RenderProcessError("runtime_unavailable")
    kernel = _windows_dll("kernel32")
    pointer = ctypes.c_void_p
    word = ctypes.c_uint32
    size = ctypes.c_size_t
    signatures = {
        "CreateJobObjectW": (pointer, (pointer, ctypes.c_wchar_p)),
        "SetInformationJobObject": (word, (pointer, word, pointer, word)),
        "QueryInformationJobObject": (word, (pointer, word, pointer, word, pointer)),
        "CloseHandle": (word, (pointer,)),
        "TerminateJobObject": (word, (pointer, word)),
        "WaitForSingleObject": (word, (pointer, word)),
        "GetExitCodeProcess": (word, (pointer, pointer)),
        "InitializeProcThreadAttributeList": (word, (pointer, word, word, pointer)),
        "UpdateProcThreadAttribute": (word, (pointer, word, size, pointer, size, pointer, pointer)),
        "DeleteProcThreadAttributeList": (None, (pointer,)),
        "CreateProcessW": (
            word,
            (
                ctypes.c_wchar_p,
                pointer,
                pointer,
                pointer,
                word,
                word,
                pointer,
                ctypes.c_wchar_p,
                pointer,
                pointer,
            ),
        ),
        "PeekNamedPipe": (word, (pointer, pointer, word, pointer, pointer, pointer)),
        "IsProcessInJob": (word, (pointer, pointer, pointer)),
        "CreateFileW": (pointer, (ctypes.c_wchar_p, word, word, pointer, word, word, pointer)),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(kernel, name)
        function.restype = result
        function.argtypes = arguments
    return kernel


def _psapi() -> Any:
    psapi = _windows_dll("psapi")
    psapi.GetProcessMemoryInfo.restype = ctypes.c_uint32
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    return psapi


class PinnedRenderExecutable:
    def __init__(
        self, path: Path, fingerprint: str, descriptor: int, scope: ExitStack, token: object
    ):
        if token is not _PIN_TOKEN:
            raise RenderProcessError("runtime_unavailable")
        self._path = path
        self.fingerprint = fingerprint
        self._descriptor = descriptor
        self._scope = scope
        metadata = os.fstat(descriptor)
        self._identity = metadata.st_dev, metadata.st_ino, metadata.st_size
        self._closed = False

        self._lock = threading.RLock()
        self._borrowers = 0

    def __repr__(self) -> str:
        return "<PinnedAuthoringRenderExecutable opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("executable pins are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("executable pins are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("executable pins are not serializable")

    def require_current(self) -> None:
        with self._lock:
            if self._closed:
                raise RenderProcessError("runtime_unavailable")
            metadata = validate_regular_file(
                self._path, maximum_bytes=_MAX_EXECUTABLE_BYTES
            ).lstat()
            if (metadata.st_dev, metadata.st_ino, metadata.st_size) != self._identity:
                raise RenderProcessError("runtime_unavailable")

    def _borrow(self) -> None:
        with self._lock:
            self.require_current()
            self._borrowers += 1

    def _release(self) -> None:
        with self._lock:
            self._borrowers -= 1
            if self._closed and not self._borrowers:
                self._scope.close()

    def __enter__(self) -> PinnedRenderExecutable:
        self.require_current()
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def close(self) -> None:
        # CRITICAL: close invalidates new admissions, but a running child still owns its
        # deny-write/delete pin. Closing the OS handle early reopens the executable race.
        with self._lock:
            self._closed = True
            if not self._borrowers:
                self._scope.close()


def pin_render_executable(
    path: Path, fingerprint: str, *, control: ProcessControl
) -> PinnedRenderExecutable:
    scope = ExitStack()
    try:
        _guard(control)
        if type(fingerprint) is not str or _DIGEST.fullmatch(fingerprint) is None:
            raise RenderProcessError("runtime_unavailable")
        admitted = validate_regular_file(path, maximum_bytes=_MAX_EXECUTABLE_BYTES)
        scope.enter_context(_validated_directories(admitted.parent))
        kernel = _kernel()
        # CRITICAL: keep deny-write/delete handles through every child run. A hash followed
        # by an ordinary reopen lets an executable change between admission and CreateProcess.
        handle = kernel.CreateFileW(str(admitted), 0x80000000, 0x1, None, 3, 0x00200080, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise RenderProcessError("runtime_unavailable")
        try:
            descriptor = _msvcrt_open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        scope.callback(os.close, descriptor)
        opened = os.fstat(descriptor)
        before = admitted.lstat()
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise RenderProcessError("runtime_unavailable")
        digest = hashlib.sha256()
        total = 0
        while True:
            _guard(control)
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > _MAX_EXECUTABLE_BYTES:
                raise RenderProcessError("runtime_unavailable")
            digest.update(block)
        if total != before.st_size or "sha256:" + digest.hexdigest() != fingerprint:
            raise RenderProcessError("runtime_unavailable")
        _guard(control)
        return PinnedRenderExecutable(admitted, fingerprint, descriptor, scope, _PIN_TOKEN)
    except BaseException as exc:
        scope.close()
        if isinstance(exc, RenderProcessError):
            raise
        if isinstance(exc, Exception):
            raise RenderProcessError("runtime_unavailable") from None
        raise


@dataclass(frozen=True, slots=True)
class RenderProcessResult:
    exit_code: int
    stdout: bytes = field(repr=False)
    diagnostic_bytes: int
    active_processes: int
    peak_committed_bytes: int
    cpu_ticks: int
    limits_verified: bool


@dataclass(frozen=True, slots=True)
class RenderProcessObservation:
    """Private acceptance observation from the process handle that was assigned to the job."""

    session_id: str
    run_id: str
    process_id: int
    executable_fingerprint: str
    job_membership_verified: bool
    working_set_valid: bool
    working_set_failure: str | None
    working_set_sample_count: int
    peak_working_set_bytes: int
    peak_committed_bytes: int
    limits_verified: bool


class WindowsRenderProcessSession:
    def __init__(
        self,
        *,
        limits: RenderJobLimits = _DEFAULT_LIMITS,
        observer: Callable[[RenderProcessObservation], None] | None = None,
    ) -> None:
        if type(limits) is not RenderJobLimits:
            raise RenderProcessError("invalid_request")
        self._kernel = _kernel()
        if observer is not None and not callable(observer):
            raise RenderProcessError("invalid_request")
        self._observer = observer
        self._psapi = _psapi() if observer is not None else None
        self._session_id = secrets.token_hex(16)
        self._limits = limits
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._job = self._kernel.CreateJobObjectW(None, None)
        self._closed = False
        if not self._job:
            raise RenderProcessError("runtime_unavailable")
        try:
            configured = _ExtendedLimits()
            configured.BasicLimitInformation.LimitFlags = _LIMIT_FLAGS
            configured.BasicLimitInformation.ActiveProcessLimit = limits.child_processes
            configured.BasicLimitInformation.PerProcessUserTimeLimit = (
                limits.child_cpu_seconds * _TICKS_PER_SECOND
            )
            configured.BasicLimitInformation.PerJobUserTimeLimit = (
                limits.child_cpu_seconds * _TICKS_PER_SECOND
            )
            configured.ProcessMemoryLimit = configured.JobMemoryLimit = limits.child_memory_bytes
            if not self._kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(configured), ctypes.sizeof(configured)
            ):
                raise RenderProcessError("runtime_unavailable")
            actual = self._extended()
            if (
                actual.BasicLimitInformation.LimitFlags != _LIMIT_FLAGS
                or actual.BasicLimitInformation.ActiveProcessLimit != limits.child_processes
                or actual.BasicLimitInformation.PerProcessUserTimeLimit
                != limits.child_cpu_seconds * _TICKS_PER_SECOND
                or actual.BasicLimitInformation.PerJobUserTimeLimit
                != limits.child_cpu_seconds * _TICKS_PER_SECOND
                or actual.ProcessMemoryLimit != limits.child_memory_bytes
                or actual.JobMemoryLimit != limits.child_memory_bytes
            ):
                raise RenderProcessError("runtime_unavailable")
        except BaseException:
            self._kernel.CloseHandle(self._job)
            self._closed = True
            raise

    def __repr__(self) -> str:
        return "<WindowsAuthoringRenderProcessSession opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("process sessions are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("process sessions are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("process sessions are not serializable")

    def __enter__(self) -> WindowsRenderProcessSession:
        if self._closed:
            raise RenderProcessError("session_closed")
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _extended(self) -> _ExtendedLimits:
        actual = _ExtendedLimits()
        if not self._kernel.QueryInformationJobObject(
            self._job, 9, ctypes.byref(actual), ctypes.sizeof(actual), None
        ):
            raise RenderProcessError("runtime_unavailable")
        return actual

    def _accounting(self) -> _Accounting:
        if self._closed:
            raise RenderProcessError("session_closed")
        actual = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self._job, 1, ctypes.byref(actual), ctypes.sizeof(actual), None
        ):
            raise RenderProcessError("runtime_unavailable")
        return actual

    @property
    def active_processes(self) -> int:
        return int(self._accounting().ActiveProcesses)

    def _check(self, control: ProcessControl) -> None:
        if self._stop.is_set():
            raise RenderProcessError("cancelled")
        _guard(control)
        accounting = self._accounting()
        if (
            accounting.TotalUserTime + accounting.TotalKernelTime
            >= self._limits.child_cpu_seconds * _TICKS_PER_SECOND
        ):
            raise RenderProcessError("resource_limit")

    def _spawn(
        self,
        executable: PinnedRenderExecutable,
        arguments: tuple[str, ...],
        cwd: Path,
        writers: tuple[int, int, int],
        information: _ProcessInformation,
    ) -> None:
        size = ctypes.c_size_t()
        self._kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not 1 <= size.value <= 65536:
            raise RenderProcessError("runtime_unavailable")
        attribute_buffer = ctypes.create_string_buffer(size.value)
        if not self._kernel.InitializeProcThreadAttributeList(
            attribute_buffer, 2, 0, ctypes.byref(size)
        ):
            raise RenderProcessError("runtime_unavailable")
        try:
            handles = (ctypes.c_void_p * 3)(
                *(_msvcrt_get_osfhandle(descriptor) for descriptor in writers)
            )
            jobs = (ctypes.c_void_p * 1)(self._job)
            for descriptor in writers:
                os.set_inheritable(descriptor, True)
            # CRITICAL: JOB_LIST applies limits before user code runs; HANDLE_LIST excludes
            # every foreign socket/file/job handle even though these three streams inherit.
            for key, value in ((0x00020002, handles), (0x0002000D, jobs)):
                if not self._kernel.UpdateProcThreadAttribute(
                    attribute_buffer, 0, key, value, ctypes.sizeof(value), None, None
                ):
                    raise RenderProcessError("runtime_unavailable")
            startup = _StartupEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100 | 0x1
            startup.StartupInfo.wShowWindow = 0
            (
                startup.StartupInfo.hStdInput,
                startup.StartupInfo.hStdOutput,
                startup.StartupInfo.hStdError,
            ) = handles
            startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p).value
            environment = {
                "SystemRoot": os.environ.get("SystemRoot", ""),
                "WINDIR": os.environ.get("SystemRoot", ""),
                "TEMP": str(cwd),
                "TMP": str(cwd),
                "AV_LOG_FORCE_NOCOLOR": "1",
            }
            if not environment["SystemRoot"]:
                raise RenderProcessError("runtime_unavailable")
            block = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
            )
            command = subprocess.list2cmdline([str(executable._path), *arguments])
            if len(command) > 32766:
                raise RenderProcessError("invalid_request")
            command_buffer = ctypes.create_unicode_buffer(command)
            executable.require_current()
            if not self._kernel.CreateProcessW(
                str(executable._path),
                command_buffer,
                None,
                None,
                True,
                0x00080000 | 0x00000400 | 0x08000000,
                block,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            ):
                raise RenderProcessError("process_failed")
        finally:
            self._kernel.DeleteProcThreadAttributeList(attribute_buffer)
            for descriptor in writers:
                os.set_inheritable(descriptor, False)

    def _observed_working_set(self, process: int) -> int | None:
        if self._psapi is None:
            return None
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if not self._psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(process), ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)

    def _job_contains(self, process: int) -> bool:
        member = ctypes.c_int()
        return bool(
            self._kernel.IsProcessInJob(ctypes.c_void_p(process), self._job, ctypes.byref(member))
            and member.value
        )

    def _read_available(self, descriptor: int) -> bytes:
        available = ctypes.c_uint32()
        if not self._kernel.PeekNamedPipe(
            _msvcrt_get_osfhandle(descriptor), None, 0, None, ctypes.byref(available), None
        ):
            error = int(getattr(ctypes, "get_last_error")())  # noqa: B009
            if error == 109:  # ERROR_BROKEN_PIPE: the only writer was the reaped owned process.
                return b""
            raise RenderProcessError("process_failed")
        return os.read(descriptor, min(4096, available.value)) if available.value else b""

    def run(
        self,
        executable: PinnedRenderExecutable,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        control: ProcessControl,
        maximum_stdout: int = 65536,
        check_staging: Callable[[], object] | None = None,
    ) -> RenderProcessResult:
        if (
            type(executable) is not PinnedRenderExecutable
            or type(arguments) is not tuple
            or len(arguments) > 4096
            or any(type(arg) is not str or len(arg) > 4096 or "\0" in arg for arg in arguments)
            or type(maximum_stdout) is not int
            or not 1 <= maximum_stdout <= self._limits.max_probe_bytes
            or (check_staging is not None and not callable(check_staging))
        ):
            raise RenderProcessError("invalid_request")
        if not self._lock.acquire(blocking=False):
            raise RenderProcessError("resource_limit")
        information = _ProcessInformation()
        descriptors: set[int] = set()
        stdout = bytearray()
        stderr_bytes = 0
        exit_code = ctypes.c_uint32()
        accounting: _Accounting | None = None
        actual: _ExtendedLimits | None = None
        borrowed = False
        observed_peak = 0
        observed_samples = 0
        observed_failure: str | None = None
        job_member = False
        run_id = secrets.token_hex(16)
        try:
            if self._closed:
                raise RenderProcessError("session_closed")
            self._check(control)
            executable._borrow()
            borrowed = True
            directory = validate_directory(cwd)
            with _validated_directories(directory):
                input_descriptor = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                descriptors.add(input_descriptor)
                stdout_read, stdout_write = os.pipe()
                descriptors.update((stdout_read, stdout_write))
                stderr_read, stderr_write = os.pipe()
                descriptors.update((stderr_read, stderr_write))
                writers = (input_descriptor, stdout_write, stderr_write)
                self._spawn(executable, arguments, directory, writers, information)
                if self._observer is not None:
                    # IMPORTANT (B-M2545-50): observe the exact process handle created inside the
                    # configured job. A parent-tree snapshot cannot prove job membership and a PID
                    # alone can be recycled before a later read.
                    job_member = self._job_contains(int(information.hProcess))
                    measured_working_set = self._observed_working_set(int(information.hProcess))
                    if measured_working_set is None:
                        observed_failure = "working_set_unavailable"
                    else:
                        observed_peak = measured_working_set
                        observed_samples = 1
                for descriptor in writers:
                    os.close(descriptor)
                    descriptors.remove(descriptor)
                exited = False
                while True:
                    self._check(control)
                    if self._observer is not None:
                        measured_working_set = self._observed_working_set(int(information.hProcess))
                        if measured_working_set is None:
                            observed_failure = "working_set_unavailable"
                        else:
                            observed_peak = max(observed_peak, measured_working_set)
                            observed_samples += 1
                    if check_staging is not None:
                        check_staging()
                    output = self._read_available(stdout_read)
                    diagnostic = self._read_available(stderr_read)
                    stderr_bytes += len(diagnostic)
                    stdout.extend(output)
                    if len(stdout) > maximum_stdout or stderr_bytes > self._limits.max_probe_bytes:
                        raise RenderProcessError("resource_limit")
                    # Drain both pipes again after observing exit. Data can arrive between
                    # the previous PeekNamedPipe and the process signal, including stderr.
                    if exited and not output and not diagnostic:
                        if not self._kernel.GetExitCodeProcess(
                            information.hProcess, ctypes.byref(exit_code)
                        ):
                            raise RenderProcessError("process_failed")
                        break
                    waited = self._kernel.WaitForSingleObject(information.hProcess, 10)
                    if waited == 0:
                        exited = True
                        continue
                    if waited != 258:
                        raise RenderProcessError("process_failed")
        except BaseException as exc:
            self._stop.set()
            stdout.clear()
            if isinstance(exc, RenderProcessError):
                raise
            if isinstance(exc, Exception):
                raise RenderProcessError("process_failed") from None
            raise
        finally:
            cleanup_failed = False
            cleanup_deadline = time.monotonic() + self._limits.cleanup_grace_seconds
            if information.hProcess:
                if self._kernel.WaitForSingleObject(information.hProcess, 0) != 0:
                    if not self._kernel.TerminateJobObject(self._job, 1):
                        cleanup_failed = True
                    if (
                        self._kernel.WaitForSingleObject(
                            information.hProcess, self._limits.cleanup_grace_seconds * 1000
                        )
                        != 0
                    ):
                        cleanup_failed = True
                if information.hThread and not self._kernel.CloseHandle(information.hThread):
                    cleanup_failed = True
                if not self._kernel.CloseHandle(information.hProcess):
                    cleanup_failed = True
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    cleanup_failed = True
            try:
                if not self._closed:
                    # CRITICAL: signaled process handles and job accounting settle separately.
                    # Keep ownership locked while waiting, or close/reuse races lose the proof.
                    accounting = self._accounting()
                    while accounting.ActiveProcesses and time.monotonic() < cleanup_deadline:
                        self._stop.wait(0.01) if not self._stop.is_set() else time.sleep(0.01)
                        accounting = self._accounting()
                    actual = self._extended()
                    cleanup_failed = cleanup_failed or bool(accounting.ActiveProcesses)
            except RenderProcessError:
                cleanup_failed = True
            finally:
                if borrowed:
                    try:
                        executable._release()
                    except OSError:
                        cleanup_failed = True
                self._lock.release()
            if cleanup_failed:
                raise RenderProcessError("cleanup_failed")
        if accounting is None or actual is None or accounting.ActiveProcesses:
            self._stop.set()
            raise RenderProcessError("cleanup_failed")
        result = RenderProcessResult(
            exit_code=int(exit_code.value),
            stdout=bytes(stdout),
            diagnostic_bytes=stderr_bytes,
            active_processes=int(accounting.ActiveProcesses),
            peak_committed_bytes=int(actual.PeakJobMemoryUsed),
            cpu_ticks=int(accounting.TotalUserTime + accounting.TotalKernelTime),
            limits_verified=True,
        )
        if self._observer is not None:
            self._observer(
                RenderProcessObservation(
                    session_id=self._session_id,
                    run_id=run_id,
                    process_id=int(information.dwProcessId),
                    executable_fingerprint=executable.fingerprint,
                    job_membership_verified=job_member,
                    working_set_valid=observed_failure is None and observed_samples > 0,
                    working_set_failure=observed_failure,
                    working_set_sample_count=observed_samples,
                    peak_working_set_bytes=observed_peak,
                    peak_committed_bytes=result.peak_committed_bytes,
                    limits_verified=result.limits_verified,
                )
            )
        return result

    def close(self) -> None:
        self._stop.set()
        if not self._lock.acquire(timeout=self._limits.cleanup_grace_seconds):
            raise RenderProcessError("cleanup_failed")
        try:
            if not self._closed:
                if self.active_processes:
                    if not self._kernel.TerminateJobObject(self._job, 1):
                        raise RenderProcessError("cleanup_failed")
                if not self._kernel.CloseHandle(self._job):
                    raise RenderProcessError("cleanup_failed")
                self._closed = True
        finally:
            self._lock.release()
