"""Optional stdlib-only FFmpeg/ffprobe subprocess seam.

The adapter is never imported by the pure package root. It accepts only explicit argument arrays,
uses ``shell=False``, bounds process output and wall time, and owns cleanup of exact temporary
outputs supplied by its caller. Tests may inject a fake process boundary; no media binary is
required for the core contract.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from comfyui_h3_context.core.errors import MediaProcessError
from comfyui_h3_context.core.media_admission import MediaAllowlist, MediaLimits

MEDIA_PROCESS_SCHEMA = "h3.media.process.v1"
OUTPUT_LEASE_SCHEMA = "h3.media.output_lease.v1"
_TOOLS = frozenset({"ffprobe", "ffmpeg"})
_MAX_ARG_COUNT = 256
_MAX_ARG_LENGTH = 4096
_SUFFIX = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_LEASE_TOKEN = object()


class ProcessStatus(str, Enum):
    """Bounded process outcomes; raw tool diagnostics remain runtime-only."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SPAWN_FAILED = "spawn_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CANCELLATION_FAILED = "cancellation_failed"
    OUTPUT_LIMIT = "output_limit"
    HANDOFF_FAILED = "handoff_failed"
    PIPE_FAILED = "pipe_failed"
    TERMINATION_FAILED = "termination_failed"
    CLEANUP_FAILED = "cleanup_failed"


class CancellationProbe(Protocol):
    """Minimal injected cancellation contract."""

    def is_cancelled(self) -> bool: ...


def _has_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validate_owned_root(root: Path) -> Path:
    if root.exists() and (root.is_symlink() or _has_reparse_point(root)):
        raise MediaProcessError("owned output root must not be a symlink or reparse point")
    try:
        root.mkdir(parents=True, exist_ok=True)
        lexical = Path(os.path.abspath(root))
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MediaProcessError("owned output root could not be prepared") from exc
    if not root.is_dir() or not _same_path(lexical, resolved):
        raise MediaProcessError("owned output root must resolve without indirection")
    return resolved


@dataclass(slots=True)
class OwnedOutputLease:
    """One output path inside an atomically allocated execution-owned directory."""

    _root: Path
    _path: Path
    schema: str = OUTPUT_LEASE_SCHEMA
    _token: object = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, init=False, repr=False, compare=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_root", "_path", "schema", "_token"} and hasattr(self, name):
            raise AttributeError("output lease ownership identity is immutable")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        if self._token is not _LEASE_TOKEN or self.schema != OUTPUT_LEASE_SCHEMA:
            raise MediaProcessError("output leases must be created by create_output_lease")
        object.__setattr__(self, "_root", self._root.resolve(strict=True))
        object.__setattr__(self, "_path", Path(os.path.abspath(self._path)))
        self._validate_container()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    @property
    def released(self) -> bool:
        return self._released

    @property
    def directory(self) -> Path:
        return self.path.parent

    def _validate_container(self) -> None:
        try:
            directory = self.directory
            resolved = directory.resolve(strict=True)
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or _has_reparse_point(directory)
                or not _same_path(resolved.parent, self.root)
                or not _same_path(resolved, directory)
                or not _same_path(self.path.parent, directory)
            ):
                raise MediaProcessError("output lease escaped its owned root")
        except (OSError, RuntimeError) as exc:
            raise MediaProcessError("output lease container is invalid") from exc

    def validate_for_spawn(self) -> None:
        with self._lock:
            if self._released:
                raise MediaProcessError("output lease has already been released")
            self._validate_container()
            if self.path.exists() or self.path.is_symlink():
                raise MediaProcessError("owned output path must not exist before process spawn")

    def inspect_artifact(self, limit: int) -> tuple[str, int]:
        with self._lock:
            if self._released:
                return "error", 0
            try:
                self._validate_container()
                if self.path.is_symlink() or _has_reparse_point(self.path):
                    return "error", 0
                if not self.path.exists():
                    return "missing", 0
                if not self.path.is_file():
                    return "error", 0
                size = self.path.stat().st_size
            except (OSError, RuntimeError, MediaProcessError):
                return "error", 0
            return ("overflow", size) if size > limit else ("ok", size)

    def release(self) -> None:
        """Delete only the verified owned artifact/directory; repeated release is harmless."""

        with self._lock:
            if self._released:
                return
            self._validate_container()
            if self.path.is_symlink() or _has_reparse_point(self.path):
                raise MediaProcessError("refusing to release an indirect output path")
            if self.path.exists():
                if not self.path.is_file():
                    raise MediaProcessError("refusing to release a non-file output")
                self.path.unlink()
            self.directory.rmdir()
            self._released = True

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "released": self.released,
            "artifact_present": self.path.is_file() if not self.released else False,
        }


def create_output_lease(root: str | Path, *, suffix: str) -> OwnedOutputLease:
    """Atomically reserve a private output namespace below an execution-owned root."""

    if not isinstance(root, (str, Path)) or not str(root) or "\x00" in str(root):
        raise MediaProcessError("owned output root is invalid")
    if not isinstance(suffix, str) or not _SUFFIX.fullmatch(suffix):
        raise MediaProcessError("owned output suffix is invalid")
    resolved_root = _validate_owned_root(Path(root))
    try:
        directory = Path(tempfile.mkdtemp(prefix="h3-media-", dir=resolved_root))
        directory.chmod(0o700)
    except OSError as exc:
        raise MediaProcessError("output lease could not be allocated") from exc
    return OwnedOutputLease(resolved_root, directory / f"artifact{suffix}", _token=_LEASE_TOKEN)


@dataclass(frozen=True, slots=True)
class MediaProcessInvocation:
    """One explicit process invocation; the argv and runtime paths are not portable fields."""

    tool: str
    argv: tuple[str, ...] = field(repr=False, compare=False)
    protocol_whitelist: tuple[str, ...]
    format_whitelist: tuple[str, ...]
    codec_whitelist: tuple[str, ...]
    timeout_seconds: Decimal
    max_stdout_bytes: int
    max_stderr_bytes: int
    executable: str | None = field(default=None, repr=False, compare=False)
    shell: bool = False
    start_new_session: bool = False
    output_leases: tuple[OwnedOutputLease, ...] = field(default=(), repr=False, compare=False)
    schema: str = MEDIA_PROCESS_SCHEMA
    max_owned_output_bytes: int = 1

    def __post_init__(self) -> None:
        if self.tool not in _TOOLS:
            raise MediaProcessError("media executable is not allowlisted")
        if not isinstance(self.argv, tuple) or not self.argv or len(self.argv) > _MAX_ARG_COUNT:
            raise MediaProcessError("media argv must be a bounded tuple")
        executable = self.tool if self.executable is None else self.executable
        if (
            not isinstance(executable, str)
            or not executable
            or len(executable) > _MAX_ARG_LENGTH
            or "\x00" in executable
            or (self.executable is not None and not os.path.isabs(executable))
        ):
            raise MediaProcessError("explicit media executable must be an absolute locator")
        if self.argv[0] != executable:
            raise MediaProcessError("argv executable does not match the selected media tool")
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_ARG_LENGTH
            or "\x00" in value
            for value in self.argv
        ):
            raise MediaProcessError("media argv contains an invalid argument")
        for values, field_name in (
            (self.protocol_whitelist, "protocol_whitelist"),
            (self.format_whitelist, "format_whitelist"),
            (self.codec_whitelist, "codec_whitelist"),
        ):
            if not isinstance(values, tuple) or not values or len(values) > 128:
                raise MediaProcessError(f"{field_name} must be a bounded tuple")
            if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
                raise MediaProcessError(f"{field_name} contains an invalid value")
            if len(values) != len(set(values)):
                raise MediaProcessError(f"{field_name} must not contain duplicates")
        if (
            not isinstance(self.timeout_seconds, Decimal)
            or not self.timeout_seconds.is_finite()
            or self.timeout_seconds <= 0
        ):
            raise MediaProcessError("timeout_seconds must be a finite positive Decimal")
        for value, field_name in (
            (self.max_stdout_bytes, "max_stdout_bytes"),
            (self.max_stderr_bytes, "max_stderr_bytes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MediaProcessError(f"{field_name} must be a positive integer")
        if self.shell is not False:
            raise MediaProcessError("media processes must use shell=False")
        if not isinstance(self.start_new_session, bool):
            raise MediaProcessError("start_new_session must be a boolean")
        if not isinstance(self.output_leases, tuple) or len(self.output_leases) > 16:
            raise MediaProcessError("output leases exceed the finite limit")
        if any(not isinstance(lease, OwnedOutputLease) for lease in self.output_leases):
            raise MediaProcessError("raw caller output paths are forbidden")
        paths = [os.path.normcase(str(lease.path)) for lease in self.output_leases]
        if len(paths) != len(set(paths)):
            raise MediaProcessError("output leases must not contain duplicates")
        if (
            isinstance(self.max_owned_output_bytes, bool)
            or not isinstance(self.max_owned_output_bytes, int)
            or self.max_owned_output_bytes <= 0
        ):
            raise MediaProcessError("max_owned_output_bytes must be a positive integer")
        if self.schema != MEDIA_PROCESS_SCHEMA:
            raise MediaProcessError("unsupported media process schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "tool": self.tool,
            "argv_count": len(self.argv),
            "protocol_whitelist": list(self.protocol_whitelist),
            "format_whitelist": list(self.format_whitelist),
            "codec_whitelist": list(self.codec_whitelist),
            "timeout_seconds": format(self.timeout_seconds, "f"),
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "shell": self.shell,
            "start_new_session": self.start_new_session,
            "owned_output_count": len(self.output_leases),
            "output_lease_schema": OUTPUT_LEASE_SCHEMA,
            "max_owned_output_bytes": self.max_owned_output_bytes,
        }


def _locator(value: str | Path) -> str:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise MediaProcessError("media locator is invalid")
    return str(value)


def _codec_values(allowlist: MediaAllowlist) -> tuple[str, ...]:
    values = tuple(
        dict.fromkeys((*allowlist.video_codecs, *allowlist.audio_codecs, *allowlist.image_codecs))
    )
    if not values:
        raise MediaProcessError("codec allowlist must not be empty")
    return values


def build_ffprobe_invocation(
    input_locator: str | Path,
    *,
    limits: MediaLimits,
    allowlist: MediaAllowlist,
) -> MediaProcessInvocation:
    """Build a bounded JSON probe command without shell interpolation."""

    locator = _locator(input_locator)
    argv = (
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        ",".join(allowlist.protocols),
        "-format_whitelist",
        ",".join(allowlist.containers),
        "-codec_whitelist",
        ",".join(_codec_values(allowlist)),
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "-i",
        locator,
    )
    return MediaProcessInvocation(
        tool="ffprobe",
        argv=argv,
        protocol_whitelist=allowlist.protocols,
        format_whitelist=allowlist.containers,
        codec_whitelist=_codec_values(allowlist),
        timeout_seconds=limits.max_wall_time_seconds,
        max_stdout_bytes=limits.max_probe_stdout_bytes,
        max_stderr_bytes=limits.max_probe_stderr_bytes,
        start_new_session=os.name == "posix",
    )


def build_ffmpeg_invocation(
    input_locator: str | Path,
    output_lease: OwnedOutputLease,
    *,
    limits: MediaLimits,
    allowlist: MediaAllowlist,
) -> MediaProcessInvocation:
    """Build an explicit bounded no-clobber decode command for an owned lease."""

    if not isinstance(output_lease, OwnedOutputLease):
        raise MediaProcessError("ffmpeg output requires an owned output lease")
    output_lease.validate_for_spawn()
    locator = _locator(input_locator)
    output = str(output_lease.path)
    argv = (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        ",".join(allowlist.protocols),
        "-format_whitelist",
        ",".join(allowlist.containers),
        "-codec_whitelist",
        ",".join(_codec_values(allowlist)),
        "-i",
        locator,
        "-map",
        "0",
        "-n",
        output,
    )
    return MediaProcessInvocation(
        tool="ffmpeg",
        argv=argv,
        protocol_whitelist=allowlist.protocols,
        format_whitelist=allowlist.containers,
        codec_whitelist=_codec_values(allowlist),
        timeout_seconds=limits.max_wall_time_seconds,
        max_stdout_bytes=limits.max_probe_stdout_bytes,
        max_stderr_bytes=limits.max_probe_stderr_bytes,
        start_new_session=os.name == "posix",
        output_leases=(output_lease,),
        max_owned_output_bytes=min(limits.max_temp_bytes, limits.max_decoded_bytes),
    )


@dataclass(frozen=True, slots=True)
class ProcessCapture:
    """Runtime capture with bounded public metadata and hidden tool bytes."""

    status: ProcessStatus | str
    stdout: bytes = field(default=b"", repr=False, compare=False)
    stderr: bytes = field(default=b"", repr=False, compare=False)
    exit_code: int | None = None
    cleanup_succeeded: bool = True
    schema: str = MEDIA_PROCESS_SCHEMA
    owned_output_bytes: int = 0
    artifacts: tuple[OwnedOutputLease, ...] = field(default=(), repr=False, compare=False)
    reaped: bool = True
    poll_observations: int = 0
    reader_threads_joined: bool = True
    cleanup_errors: int = 0

    def __post_init__(self) -> None:
        try:
            status = (
                self.status
                if isinstance(self.status, ProcessStatus)
                else ProcessStatus(self.status)
            )
        except (TypeError, ValueError):
            raise MediaProcessError("unsupported media process status") from None
        object.__setattr__(self, "status", status)
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise MediaProcessError("process output must be bytes")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise MediaProcessError("exit_code must be an integer or None")
        if not isinstance(self.cleanup_succeeded, bool):
            raise MediaProcessError("cleanup_succeeded must be a boolean")
        _owned_bytes = self.owned_output_bytes
        if isinstance(_owned_bytes, bool) or not isinstance(_owned_bytes, int) or _owned_bytes < 0:
            raise MediaProcessError("owned_output_bytes must be a non-negative integer")
        for value, field_name in (
            (self.poll_observations, "poll_observations"),
            (self.cleanup_errors, "cleanup_errors"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MediaProcessError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(lease, OwnedOutputLease) for lease in self.artifacts
        ):
            raise MediaProcessError("artifacts must contain owned output leases")
        if status is not ProcessStatus.SUCCEEDED and self.artifacts:
            raise MediaProcessError("failed process must not publish artifacts")
        if not isinstance(self.reaped, bool) or not isinstance(self.reader_threads_joined, bool):
            raise MediaProcessError("process lifecycle flags must be boolean")
        expected_cleanup = self.reaped and self.reader_threads_joined and self.cleanup_errors == 0
        if self.cleanup_succeeded is not expected_cleanup:
            raise MediaProcessError("cleanup success contradicts process lifecycle evidence")
        if self.schema != MEDIA_PROCESS_SCHEMA:
            raise MediaProcessError("unsupported media process schema")

    def release_artifacts(self) -> bool:
        success = True
        for lease in self.artifacts:
            try:
                lease.release()
            except (OSError, RuntimeError, MediaProcessError):
                success = False
        return success

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": cast(ProcessStatus, self.status).value,
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "exit_code": self.exit_code,
            "cleanup_succeeded": self.cleanup_succeeded,
            "owned_output_bytes": self.owned_output_bytes,
            "artifact_count": len(self.artifacts),
            "reaped": self.reaped,
            "poll_observations": self.poll_observations,
            "reader_threads_joined": self.reader_threads_joined,
            "cleanup_errors": self.cleanup_errors,
        }


def _windows_system_executable(filename: str) -> str | None:
    """Resolve one fixed Windows system executable without consulting ambient PATH."""

    if os.name != "nt" or filename != "taskkill.exe":
        return None
    try:
        import ctypes

        # CRITICAL: Linux Python stubs omit Windows-only ctypes symbols; this branch is reached
        # only for the explicitly gated Windows cleanup path.
        win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
        kernel32 = win_dll("kernel32", use_last_error=True)
        get_system_directory = kernel32.GetSystemDirectoryW
        get_system_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        get_system_directory.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(get_system_directory(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            return None
        # SECURITY: cancellation cleanup must never execute a PATH-selected helper.
        candidate = (Path(buffer.value) / filename).resolve(strict=True)
        state = candidate.lstat()
        if (
            not candidate.is_absolute()
            or not stat.S_ISREG(state.st_mode)
            or _has_reparse_point(candidate)
        ):
            return None
        return str(candidate)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None


def _terminate_process(process: subprocess.Popen[bytes], *, force: bool = False) -> bool:
    """Terminate the exact process group/tree created for this invocation."""

    if process.poll() is not None:
        return True
    try:
        if os.name == "posix":
            killpg = getattr(os, "killpg", None)
            sig = getattr(signal, "SIGKILL" if force else "SIGTERM", None)
            if killpg is None or sig is None:
                return False
            killpg(process.pid, sig)
        elif os.name == "nt":
            # CRITICAL: terminate the owned Windows tree, not only the immediate process.
            taskkill = _windows_system_executable("taskkill.exe")
            if taskkill is None:
                return False
            result = subprocess.run(  # noqa: S603
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=2.0,
            )
            if result.returncode != 0 and process.poll() is None:
                process.kill() if force else process.terminate()
        elif force:
            process.kill()
        else:
            process.terminate()
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        return process.poll() is not None
    return True


def _owned_output_state(leases: tuple[OwnedOutputLease, ...], limit: int) -> tuple[str, int]:
    """Inspect verified lease artifacts and return ok/missing/overflow/error."""

    total = 0
    missing = False
    for lease in leases:
        state, size = lease.inspect_artifact(limit)
        total += size
        if total > limit or state == "overflow":
            return "overflow", total
        if state == "error":
            return "error", total
        if state == "missing":
            missing = True
    return ("missing" if missing else "ok"), total


def _read_limited(stream: BinaryIO, limit: int, result: dict[str, object]) -> None:
    """Read at most ``limit + 1`` bytes so a bombed tool cannot grow an unbounded buffer."""

    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            remaining = limit + 1 - total
            if remaining <= 0:
                result["overflow"] = True
                break
            chunk = stream.read(min(65_536, remaining))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                result["error"] = True
                break
            total += len(chunk)
            chunks.append(chunk)
            if total > limit:
                result["overflow"] = True
                break
    except (OSError, ValueError):
        result["error"] = True
    result["data"] = b"".join(chunks)


def _close_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class SubprocessMediaRunner:
    """Bounded runner used only when a caller explicitly selects a media process."""

    def run(
        self,
        invocation: MediaProcessInvocation,
        cancellation: CancellationProbe | None = None,
    ) -> ProcessCapture:
        if not isinstance(invocation, MediaProcessInvocation):
            raise MediaProcessError("invocation must be MediaProcessInvocation")
        for lease in invocation.output_leases:
            lease.validate_for_spawn()
        process: subprocess.Popen[bytes] | None = None
        threads: tuple[threading.Thread, ...] = ()
        stdout_result: dict[str, object] = {}
        stderr_result: dict[str, object] = {}
        status = ProcessStatus.FAILED
        stdout = b""
        stderr = b""
        exit_code: int | None = None
        owned_output_bytes = 0
        poll_observations = 0
        reaped = True
        readers_joined = True
        cleanup_errors = 0
        try:
            if cancellation is not None:
                try:
                    if cancellation.is_cancelled():
                        status = ProcessStatus.CANCELLED
                except Exception:
                    status = ProcessStatus.CANCELLATION_FAILED
            if status is ProcessStatus.FAILED:
                try:
                    if os.name == "nt":
                        # CRITICAL: a new process group is required for Windows tree ownership.
                        process = subprocess.Popen(  # noqa: S603
                            invocation.argv,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=False,
                            start_new_session=False,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        )
                    else:
                        process = subprocess.Popen(  # noqa: S603
                            invocation.argv,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=False,
                            start_new_session=invocation.start_new_session,
                        )
                except (OSError, ValueError):
                    status = ProcessStatus.SPAWN_FAILED
            if process is not None and (process.stdout is None or process.stderr is None):
                status = ProcessStatus.PIPE_FAILED
            elif process is not None:
                threads = (
                    threading.Thread(
                        target=_read_limited,
                        args=(
                            cast(BinaryIO, process.stdout),
                            invocation.max_stdout_bytes,
                            stdout_result,
                        ),
                        daemon=False,
                    ),
                    threading.Thread(
                        target=_read_limited,
                        args=(
                            cast(BinaryIO, process.stderr),
                            invocation.max_stderr_bytes,
                            stderr_result,
                        ),
                        daemon=False,
                    ),
                )
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + float(invocation.timeout_seconds)
                while process.poll() is None:
                    poll_observations += 1
                    output_state, owned_output_bytes = _owned_output_state(
                        invocation.output_leases, invocation.max_owned_output_bytes
                    )
                    if (
                        stdout_result.get("overflow")
                        or stderr_result.get("overflow")
                        or output_state in {"overflow", "error"}
                    ):
                        status = ProcessStatus.OUTPUT_LIMIT
                        break
                    if cancellation is not None:
                        try:
                            if cancellation.is_cancelled():
                                status = ProcessStatus.CANCELLED
                                break
                        except Exception:
                            status = ProcessStatus.CANCELLATION_FAILED
                            break
                    if time.monotonic() >= deadline:
                        status = ProcessStatus.TIMED_OUT
                        break
                    time.sleep(0.01)
                if process.poll() is None:
                    if not _terminate_process(process):
                        status = ProcessStatus.TERMINATION_FAILED
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        _terminate_process(process, force=True)
                        try:
                            process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            reaped = False
                            if status is ProcessStatus.FAILED:
                                status = ProcessStatus.TERMINATION_FAILED
                else:
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        reaped = False
                for thread in threads:
                    thread.join(timeout=1.0)
                if any(thread.is_alive() for thread in threads):
                    _close_streams(process)
                    for thread in threads:
                        thread.join(timeout=1.0)
                readers_joined = not any(thread.is_alive() for thread in threads)
                stdout = cast(bytes, stdout_result.get("data", b""))
                stderr = cast(bytes, stderr_result.get("data", b""))
                exit_code = process.returncode
                output_state, owned_output_bytes = _owned_output_state(
                    invocation.output_leases, invocation.max_owned_output_bytes
                )
                if stdout_result.get("overflow") or stderr_result.get("overflow"):
                    status = ProcessStatus.OUTPUT_LIMIT
                elif stdout_result.get("error") or stderr_result.get("error"):
                    if status is ProcessStatus.FAILED:
                        status = ProcessStatus.PIPE_FAILED
                elif not readers_joined and status is ProcessStatus.FAILED:
                    status = ProcessStatus.PIPE_FAILED
                elif status is ProcessStatus.FAILED:
                    status = ProcessStatus.SUCCEEDED if exit_code == 0 else ProcessStatus.FAILED
                if status is ProcessStatus.SUCCEEDED and invocation.output_leases:
                    if output_state == "overflow":
                        status = ProcessStatus.OUTPUT_LIMIT
                    elif output_state != "ok":
                        status = ProcessStatus.HANDOFF_FAILED
                elif output_state in {"overflow", "error"}:
                    status = ProcessStatus.OUTPUT_LIMIT
        finally:
            if process is not None:
                if process.poll() is None:
                    _terminate_process(process, force=True)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        reaped = False
                _close_streams(process)
            if status is not ProcessStatus.SUCCEEDED:
                for lease in invocation.output_leases:
                    try:
                        lease.release()
                    except (OSError, RuntimeError, MediaProcessError):
                        cleanup_errors += 1
        cleanup_errors += int(not reaped) + int(not readers_joined)
        cleanup_succeeded = cleanup_errors == 0
        artifacts = invocation.output_leases if status is ProcessStatus.SUCCEEDED else ()
        return ProcessCapture(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            cleanup_succeeded=cleanup_succeeded,
            owned_output_bytes=owned_output_bytes,
            artifacts=artifacts,
            reaped=reaped,
            poll_observations=poll_observations,
            reader_threads_joined=readers_joined,
            cleanup_errors=cleanup_errors,
        )


__all__ = [
    "MEDIA_PROCESS_SCHEMA",
    "OUTPUT_LEASE_SCHEMA",
    "CancellationProbe",
    "MediaProcessInvocation",
    "OwnedOutputLease",
    "ProcessCapture",
    "ProcessStatus",
    "SubprocessMediaRunner",
    "build_ffmpeg_invocation",
    "build_ffprobe_invocation",
    "create_output_lease",
]
