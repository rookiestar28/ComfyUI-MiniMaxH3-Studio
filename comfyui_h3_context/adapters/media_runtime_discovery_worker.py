"""Candidate admission for media runtime discovery, run inside one supervised worker process.

The resolver in `media_runtime_resolution` chooses *which* directories to consider, in which order
and under which budget; this module is the only place that touches those directories. It runs in a
short-lived repository-owned Python process so that a filesystem call that blocks -- a stalled
network share, an antivirus scan holding a handle -- can be abandoned by killing the process, which
a thread timeout cannot do.

Nothing here executes a candidate. A pair is admitted only when both fixed basenames are present in
one directory and both pass the shared exact-digest pin, the same primitive every later media spawn
repeats. The protocol is closed, bounded JSON on stdin/stdout and carries no exception text.
"""

from __future__ import annotations

import json
import ntpath
import os
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from ..core.safe_paths import UnsafePathError, validate_directory
from .executable_admission import (
    MAX_EXECUTABLE_BYTES,
    ExecutableAdmissionError,
    FileIdentity,
    pin_exact_executable,
)

REQUEST_SCHEMA = "h3.context.media_runtime_discovery_request.v1"
RESPONSE_SCHEMA = "h3.context.media_runtime_discovery_response.v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CANDIDATE_DIRECTORIES = 32
MAX_HASHED_PAIRS = 8
MAX_HASH_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOCATOR_CHARS = 4096
FFMPEG_NAME = "ffmpeg.exe"
FFPROBE_NAME = "ffprobe.exe"
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 6})  # removable, fixed, RAM disk
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_SHA256_HEX = frozenset("0123456789abcdefABCDEF")


class DiscoveryProtocolError(ValueError):
    """A request or response that is not exactly the closed protocol."""


class DiscoveryMode(str, Enum):
    AUTO = "auto"
    EXPLICIT = "explicit"


class DiscoveryOutcome(str, Enum):
    ADMITTED = "admitted"
    EXHAUSTED = "exhausted"
    LIMIT = "limit"
    TIMEOUT = "timeout"
    UNSAFE_PATH = "unsafe_path"
    UNSUPPORTED_PAIR = "unsupported_pair"
    SCRATCH_UNSAFE = "scratch_unsafe"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    mode: DiscoveryMode
    candidates: tuple[str, ...]
    ffmpeg_sha256: str
    ffprobe_sha256: str
    budget_ms: int
    scratch: str | None = None
    max_hashed_pairs: int = MAX_HASHED_PAIRS
    max_hash_bytes: int = MAX_HASH_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DiscoveryMode):
            raise DiscoveryProtocolError("mode")
        if type(self.candidates) is not tuple or not (
            1 <= len(self.candidates) <= MAX_CANDIDATE_DIRECTORIES
        ):
            raise DiscoveryProtocolError("candidates")
        if self.mode is DiscoveryMode.EXPLICIT and len(self.candidates) != 1:
            raise DiscoveryProtocolError("candidates")
        if any(lexical_local_directory(value) is None for value in self.candidates):
            raise DiscoveryProtocolError("candidates")
        for digest in (self.ffmpeg_sha256, self.ffprobe_sha256):
            if type(digest) is not str or len(digest) != 64 or not set(digest) <= _SHA256_HEX:
                raise DiscoveryProtocolError("digest")
        if type(self.budget_ms) is not int or not 1 <= self.budget_ms <= 60_000:
            raise DiscoveryProtocolError("budget")
        if self.scratch is not None and lexical_local_directory(self.scratch) is None:
            raise DiscoveryProtocolError("scratch")
        if type(self.max_hashed_pairs) is not int or not 1 <= self.max_hashed_pairs <= (
            MAX_HASHED_PAIRS
        ):
            raise DiscoveryProtocolError("max_hashed_pairs")
        if type(self.max_hash_bytes) is not int or not 1 <= self.max_hash_bytes <= MAX_HASH_BYTES:
            raise DiscoveryProtocolError("max_hash_bytes")

    def __repr__(self) -> str:
        return f"<DiscoveryRequest mode={self.mode.value} candidates={len(self.candidates)}>"


@dataclass(frozen=True, slots=True)
class DiscoveryResponse:
    outcome: DiscoveryOutcome
    index: int | None = None
    ffmpeg_identity: FileIdentity | None = None
    ffprobe_identity: FileIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, DiscoveryOutcome):
            raise DiscoveryProtocolError("outcome")
        admitted = self.outcome is DiscoveryOutcome.ADMITTED
        values = (self.index, self.ffmpeg_identity, self.ffprobe_identity)
        if admitted != all(value is not None for value in values):
            raise DiscoveryProtocolError("admitted")
        if not admitted and any(value is not None for value in values):
            raise DiscoveryProtocolError("admitted")
        if self.index is not None and (
            type(self.index) is not int or not 0 <= self.index < MAX_CANDIDATE_DIRECTORIES
        ):
            raise DiscoveryProtocolError("index")
        for identity in (self.ffmpeg_identity, self.ffprobe_identity):
            if identity is not None and (
                type(identity) is not tuple
                or len(identity) != 4
                or any(type(part) is not int or part < 0 for part in identity)
            ):
                raise DiscoveryProtocolError("identity")


def lexical_local_directory(value: object) -> PureWindowsPath | None:
    """Return a drive-absolute Windows directory locator, or `None` if it is not one.

    Purely lexical: no filesystem access, no variable expansion, no quote stripping. UNC, device,
    drive-relative and dot-segment forms, alternate data streams, reserved device names and
    trailing space/dot segments (which Windows silently rewrites) are all refused.
    """

    if type(value) is not str or not 3 <= len(value) <= MAX_LOCATOR_CHARS:
        return None
    if any(character in value for character in '\x00"%*?<>|') or value.startswith(("\\\\", "//")):
        return None
    drive, rest = ntpath.splitdrive(value)
    if len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha() or drive[1] != ":":
        return None
    if not rest.startswith(("\\", "/")) or ":" in rest:
        return None
    segments = [segment for segment in rest.replace("/", "\\").split("\\")]
    inner = segments[1:]
    if inner and inner[-1] == "":
        inner = inner[:-1]
    for segment in inner:
        if segment in {"", ".", ".."} or segment.endswith((" ", ".")):
            return None
        if segment.split(".", 1)[0].casefold() in _RESERVED_NAMES:
            return None
    return PureWindowsPath(drive.upper() + "\\" + "\\".join(inner))


def locator_identity(value: PureWindowsPath) -> str:
    """Case-insensitive comparison identity for a lexical Windows locator."""

    return ntpath.normcase(str(value)).rstrip("\\")


def _windows_drive_type(path: PureWindowsPath) -> int:
    if os.name != "nt":
        return 0
    import ctypes

    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
    kernel32 = win_dll("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = (ctypes.c_wchar_p,)
    get_drive_type.restype = ctypes.c_uint32
    return int(get_drive_type(path.drive + "\\"))


class _Budget:
    def __init__(self, request: DiscoveryRequest, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._deadline = clock() + request.budget_ms / 1000
        self._remaining_bytes = request.max_hash_bytes
        self._remaining_pairs = request.max_hashed_pairs

    def check_time(self) -> None:
        if self._clock() >= self._deadline:
            raise ExecutableAdmissionError("discovery_timeout")

    def reserve_pair(self, total_bytes: int) -> None:
        # CRITICAL: the byte budget is charged before hashing from the pair's size and again while
        # reading. Checking only the initial stat lets a file that grows between stat and read
        # overrun the resolution's I/O ceiling; checking only while reading hashes most of an
        # oversized pair before refusing it.
        if self._remaining_pairs <= 0 or total_bytes > self._remaining_bytes:
            raise ExecutableAdmissionError("discovery_limit")
        self._remaining_pairs -= 1

    def charge(self, size: int) -> None:
        self.check_time()
        self._remaining_bytes -= size
        if self._remaining_bytes < 0:
            raise ExecutableAdmissionError("discovery_limit")


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(path.is_symlink() or stat.S_ISLNK(metadata.st_mode) or attributes & reparse)


def _local_directory(
    locator: PureWindowsPath,
    drive_type: Callable[[PureWindowsPath], int],
) -> Path | None:
    if drive_type(locator) not in _LOCAL_DRIVE_TYPES:
        return None
    try:
        return validate_directory(Path(str(locator)))
    except UnsafePathError:
        return None


def _candidate_sizes(directory: Path) -> tuple[int, int] | None:
    sizes: list[int] = []
    for name in (FFMPEG_NAME, FFPROBE_NAME):
        path = directory / name
        try:
            metadata = path.lstat()
        except OSError:
            return None
        if (
            _is_link_or_reparse(path, metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_EXECUTABLE_BYTES
        ):
            return None
        sizes.append(metadata.st_size)
    return sizes[0], sizes[1]


def _scratch_is_safe(
    locator: PureWindowsPath,
    drive_type: Callable[[PureWindowsPath], int],
) -> bool:
    """Existing components of a scratch locator are local, link-free directories.

    Missing trailing components are allowed: scratch is created by its consumer on first use, never
    by discovery.
    """

    if drive_type(locator) not in _LOCAL_DRIVE_TYPES:
        return False
    current = Path(locator.anchor)
    for part in PureWindowsPath(locator).parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if _is_link_or_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
            return False
    return True


def admit_request(
    request: DiscoveryRequest,
    *,
    clock: Callable[[], float] = time.monotonic,
    drive_type: Callable[[PureWindowsPath], int] = _windows_drive_type,
) -> DiscoveryResponse:
    """Walk the ordered candidates and admit the first complete supported pair."""

    budget = _Budget(request, clock)
    explicit = request.mode is DiscoveryMode.EXPLICIT
    try:
        if request.scratch is not None:
            scratch = lexical_local_directory(request.scratch)
            if scratch is None or not _scratch_is_safe(scratch, drive_type):
                return DiscoveryResponse(DiscoveryOutcome.SCRATCH_UNSAFE)
        for index, raw in enumerate(request.candidates):
            budget.check_time()
            locator = lexical_local_directory(raw)
            directory = None if locator is None else _local_directory(locator, drive_type)
            if directory is None:
                if explicit:
                    return DiscoveryResponse(DiscoveryOutcome.UNSAFE_PATH)
                continue
            sizes = _candidate_sizes(directory)
            if sizes is None:
                # Single binaries and incomplete pairs are never hashed.
                if explicit:
                    return DiscoveryResponse(DiscoveryOutcome.UNSUPPORTED_PAIR)
                continue
            budget.reserve_pair(sizes[0] + sizes[1])
            try:
                with pin_exact_executable(
                    directory / FFMPEG_NAME, request.ffmpeg_sha256, charge=budget.charge
                ) as ffmpeg_identity:
                    pass
                with pin_exact_executable(
                    directory / FFPROBE_NAME, request.ffprobe_sha256, charge=budget.charge
                ) as ffprobe_identity:
                    pass
            except ExecutableAdmissionError as exc:
                if exc.code in {"discovery_limit", "discovery_timeout"}:
                    raise
                if explicit:
                    return DiscoveryResponse(DiscoveryOutcome.UNSUPPORTED_PAIR)
                continue
            return DiscoveryResponse(
                DiscoveryOutcome.ADMITTED,
                index=index,
                ffmpeg_identity=ffmpeg_identity,
                ffprobe_identity=ffprobe_identity,
            )
    except ExecutableAdmissionError as exc:
        if exc.code == "discovery_timeout":
            return DiscoveryResponse(DiscoveryOutcome.TIMEOUT)
        if exc.code == "discovery_limit":
            return DiscoveryResponse(DiscoveryOutcome.LIMIT)
        return DiscoveryResponse(DiscoveryOutcome.FAILURE)
    return DiscoveryResponse(DiscoveryOutcome.EXHAUSTED)


def _reject_duplicates(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryProtocolError("duplicate")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise DiscoveryProtocolError("constant")


def _decode_object(payload: bytes, maximum: int) -> Mapping[str, object]:
    if type(payload) is not bytes or not 0 < len(payload) <= maximum:
        raise DiscoveryProtocolError("size")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DiscoveryProtocolError("json") from exc
    if type(decoded) is not dict:
        raise DiscoveryProtocolError("shape")
    return decoded


def _encode(value: Mapping[str, object], maximum: int) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > maximum:
        raise DiscoveryProtocolError("size")
    return payload


def encode_request(request: DiscoveryRequest) -> bytes:
    return _encode(
        {
            "schema": REQUEST_SCHEMA,
            "mode": request.mode.value,
            "candidates": list(request.candidates),
            "ffmpeg_sha256": request.ffmpeg_sha256,
            "ffprobe_sha256": request.ffprobe_sha256,
            "budget_ms": request.budget_ms,
            "scratch": request.scratch,
            "max_hashed_pairs": request.max_hashed_pairs,
            "max_hash_bytes": request.max_hash_bytes,
        },
        MAX_REQUEST_BYTES,
    )


def decode_request(payload: bytes) -> DiscoveryRequest:
    value = _decode_object(payload, MAX_REQUEST_BYTES)
    expected = {
        "schema",
        "mode",
        "candidates",
        "ffmpeg_sha256",
        "ffprobe_sha256",
        "budget_ms",
        "scratch",
        "max_hashed_pairs",
        "max_hash_bytes",
    }
    if set(value) != expected or value["schema"] != REQUEST_SCHEMA:
        raise DiscoveryProtocolError("shape")
    candidates = value["candidates"]
    if type(candidates) is not list or any(type(item) is not str for item in candidates):
        raise DiscoveryProtocolError("candidates")
    try:
        mode = DiscoveryMode(value["mode"])
    except ValueError as exc:
        raise DiscoveryProtocolError("mode") from exc
    scratch = value["scratch"]
    if scratch is not None and type(scratch) is not str:
        raise DiscoveryProtocolError("scratch")
    return DiscoveryRequest(
        mode=mode,
        candidates=tuple(candidates),
        ffmpeg_sha256=value["ffmpeg_sha256"],  # type: ignore[arg-type]
        ffprobe_sha256=value["ffprobe_sha256"],  # type: ignore[arg-type]
        budget_ms=value["budget_ms"],  # type: ignore[arg-type]
        scratch=scratch,
        max_hashed_pairs=value["max_hashed_pairs"],  # type: ignore[arg-type]
        max_hash_bytes=value["max_hash_bytes"],  # type: ignore[arg-type]
    )


def encode_response(response: DiscoveryResponse) -> bytes:
    return _encode(
        {
            "schema": RESPONSE_SCHEMA,
            "outcome": response.outcome.value,
            "index": response.index,
            "ffmpeg_identity": (
                None if response.ffmpeg_identity is None else list(response.ffmpeg_identity)
            ),
            "ffprobe_identity": (
                None if response.ffprobe_identity is None else list(response.ffprobe_identity)
            ),
        },
        MAX_RESPONSE_BYTES,
    )


def _identity_value(value: object) -> FileIdentity | None:
    if value is None:
        return None
    if type(value) is not list or len(value) != 4 or any(type(part) is not int for part in value):
        raise DiscoveryProtocolError("identity")
    return (value[0], value[1], value[2], value[3])


def decode_response(payload: bytes) -> DiscoveryResponse:
    value = _decode_object(payload, MAX_RESPONSE_BYTES)
    if set(value) != {"schema", "outcome", "index", "ffmpeg_identity", "ffprobe_identity"}:
        raise DiscoveryProtocolError("shape")
    if value["schema"] != RESPONSE_SCHEMA:
        raise DiscoveryProtocolError("shape")
    try:
        outcome = DiscoveryOutcome(value["outcome"])
    except ValueError as exc:
        raise DiscoveryProtocolError("outcome") from exc
    index = value["index"]
    if index is not None and type(index) is not int:
        raise DiscoveryProtocolError("index")
    return DiscoveryResponse(
        outcome,
        index=index,
        ffmpeg_identity=_identity_value(value["ffmpeg_identity"]),
        ffprobe_identity=_identity_value(value["ffprobe_identity"]),
    )


def main() -> int:
    """Worker entry: one request in, one response out, no exception text on any stream."""

    try:
        request = decode_request(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        response = admit_request(request)
    except Exception:
        response = DiscoveryResponse(DiscoveryOutcome.FAILURE)
    sys.stdout.buffer.write(encode_response(response))
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "FFMPEG_NAME",
    "FFPROBE_NAME",
    "MAX_CANDIDATE_DIRECTORIES",
    "MAX_HASHED_PAIRS",
    "MAX_HASH_BYTES",
    "MAX_LOCATOR_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "DiscoveryMode",
    "DiscoveryOutcome",
    "DiscoveryProtocolError",
    "DiscoveryRequest",
    "DiscoveryResponse",
    "admit_request",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "lexical_local_directory",
    "locator_identity",
]
