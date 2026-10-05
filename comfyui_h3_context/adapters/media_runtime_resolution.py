"""Zero-input resolution of the qualified media tool pair and its private configuration.

This service answers one question -- which admitted ffmpeg/ffprobe pair, if any, should the media
features use -- without asking the user for anything. It does not publish an adapter, construct a
media store, activate rendering or download a tool. Its result proves only that a complete pair
matched the program's pinned profile at discovery time; every later spawn repeats the exact pin.

Precedence is fixed: explicit legacy locators, then the persisted local directory, then the
H3-managed profile directory, then the active interpreter prefix, then absolute PATH directories.
An explicit or persisted selection is authoritative and never falls back. Filesystem work runs in
one supervised worker process (`media_runtime_discovery_worker`) under a finite budget, and
concurrent identical queries share one result.

Private state lives under the host's system-user directory, which ComfyUI does not serve over
HTTP. There is no fallback into ComfyUI temp, input, output, the repository or browser storage.
"""

from __future__ import annotations

import json
import ntpath
import os
import platform
import secrets
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Protocol

from ..core.av_reconstruction import qualified_ffmpeg_capability
from ..core.safe_paths import UnsafePathError, ensure_directory, read_regular_file_bytes
from .composition_root import MEDIA_RUNTIME_RESOLVER, component
from .media_runtime_discovery_worker import (
    FFMPEG_NAME,
    FFPROBE_NAME,
    MAX_CANDIDATE_DIRECTORIES,
    MAX_LOCATOR_CHARS,
    MAX_RESPONSE_BYTES,
    DiscoveryMode,
    DiscoveryOutcome,
    DiscoveryProtocolError,
    DiscoveryRequest,
    DiscoveryResponse,
    decode_response,
    encode_request,
    lexical_local_directory,
    locator_identity,
)
from .segment_artifact_store import (
    ArtifactStoreError,
    _identity,
    _safe_unlink_pinned,
    _validated_directories,
    _write_new_file,
)

RESOLUTION_SCHEMA = "h3.context.media_runtime_resolution.v1"
CONFIG_SCHEMA = "h3.context.media_runtime_config.v1"
PRIVATE_ROOT_NAME = "h3_context"
MANAGED_PROFILE_COMPONENT = "gyan-full-2026-02-26-git-6695528af6"
MAX_PATH_SNAPSHOT_BYTES = 32 * 1024
MAX_CONFIG_BYTES = 16 * 1024
RESOLUTION_DEADLINE_SECONDS = 30.0
WORKER_REAP_SECONDS = 5.0
POSITIVE_CACHE_SECONDS = 60.0
NEGATIVE_CACHE_SECONDS = 5.0
LEGACY_MEDIA_MARKER = "H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME"
LEGACY_RENDER_MARKER = "H3_CONTEXT_AUTHORIZED_RENDER_RUNTIME"
LEGACY_FFMPEG = "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH"
LEGACY_FFPROBE = "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH"
LEGACY_SCRATCH = "H3_CONTEXT_AUTHORIZED_MEDIA_SCRATCH_ROOT"
LEGACY_NAMES = (
    LEGACY_MEDIA_MARKER,
    LEGACY_RENDER_MARKER,
    LEGACY_FFMPEG,
    LEGACY_FFPROBE,
    LEGACY_SCRATCH,
)
_MEDIA_RUNTIME_DIRECTORY = "media-runtime"
_CONFIG_FILENAME = "config.json"
_SUPPORTED_MACHINES = frozenset({"amd64", "x86_64"})


class ResolutionState(str, Enum):
    LOCATED = "located"
    UNAVAILABLE = "unavailable"
    INVALID_CONFIG = "invalid_config"
    DISCOVERING = "discovering"


class ResolutionReason(str, Enum):
    PAIR_ADMITTED = "pair_admitted"
    SUPPORTED_PAIR_MISSING = "supported_pair_missing"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNSUPPORTED_HOST = "unsupported_host"
    PRIVATE_ROOT_INVALID = "private_root_invalid"
    OVERRIDE_INCOMPLETE = "override_incomplete"
    OVERRIDE_INVALID_MARKER = "override_invalid_marker"
    OVERRIDE_INVALID_PATH = "override_invalid_path"
    OVERRIDE_SPLIT_DIRECTORY = "override_split_directory"
    OVERRIDE_UNSUPPORTED_PAIR = "override_unsupported_pair"
    LOCAL_SELECTION_INVALID_PATH = "local_selection_invalid_path"
    LOCAL_SELECTION_UNSUPPORTED_PAIR = "local_selection_unsupported_pair"
    CONFIG_CORRUPT = "config_corrupt"
    CONFIG_UNSUPPORTED = "config_unsupported"
    DISCOVERY_LIMIT = "discovery_limit"
    DISCOVERY_TIMEOUT = "discovery_timeout"
    WORKER_FAILURE = "worker_failure"
    DISCOVERY_IN_PROGRESS = "discovery_in_progress"


class SourceKind(str, Enum):
    NONE = "none"
    EXPLICIT_OVERRIDE = "explicit_override"
    LOCAL_SELECTION = "local_selection"
    MANAGED = "managed"
    PYTHON_PREFIX = "python_prefix"
    SYSTEM_PATH = "system_path"


_U = ResolutionState.UNAVAILABLE
_I = ResolutionState.INVALID_CONFIG
#: The one state each reason may be reported under. `private_root_invalid` is an administrator
#: error only when it refuses an explicitly configured scratch root.
_REASON_STATES: Mapping[ResolutionReason, frozenset[ResolutionState]] = {
    ResolutionReason.PAIR_ADMITTED: frozenset({ResolutionState.LOCATED}),
    ResolutionReason.SUPPORTED_PAIR_MISSING: frozenset({_U}),
    ResolutionReason.UNSUPPORTED_PLATFORM: frozenset({_U}),
    ResolutionReason.UNSUPPORTED_HOST: frozenset({_U}),
    ResolutionReason.PRIVATE_ROOT_INVALID: frozenset({_U, _I}),
    ResolutionReason.OVERRIDE_INCOMPLETE: frozenset({_I}),
    ResolutionReason.OVERRIDE_INVALID_MARKER: frozenset({_I}),
    ResolutionReason.OVERRIDE_INVALID_PATH: frozenset({_I}),
    ResolutionReason.OVERRIDE_SPLIT_DIRECTORY: frozenset({_I}),
    ResolutionReason.OVERRIDE_UNSUPPORTED_PAIR: frozenset({_I}),
    ResolutionReason.LOCAL_SELECTION_INVALID_PATH: frozenset({_I}),
    ResolutionReason.LOCAL_SELECTION_UNSUPPORTED_PAIR: frozenset({_I}),
    ResolutionReason.CONFIG_CORRUPT: frozenset({_I}),
    ResolutionReason.CONFIG_UNSUPPORTED: frozenset({_I}),
    ResolutionReason.DISCOVERY_LIMIT: frozenset({_U}),
    ResolutionReason.DISCOVERY_TIMEOUT: frozenset({_U}),
    ResolutionReason.WORKER_FAILURE: frozenset({_U}),
    ResolutionReason.DISCOVERY_IN_PROGRESS: frozenset({ResolutionState.DISCOVERING}),
}

FileIdentity = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True, repr=False)
class MediaRuntimeResolution:
    """Host-private resolution. Its repr and wire projection never carry a locator."""

    state: ResolutionState
    reason: ResolutionReason
    source_kind: SourceKind = SourceKind.NONE
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    scratch_root: Path | None = None
    ffmpeg_identity: FileIdentity | None = None
    ffprobe_identity: FileIdentity | None = None
    profile: str = MANAGED_PROFILE_COMPONENT
    #: A `discovery_limit` whose bounded search still examined the managed profile directory (it
    #: is always the first candidate) and admitted no pair there. Private; never on the wire.
    managed_searched: bool = False

    def __post_init__(self) -> None:
        if self.state not in _REASON_STATES[self.reason]:
            raise ValueError("resolution state does not match its reason")
        if self.managed_searched and self.reason is not ResolutionReason.DISCOVERY_LIMIT:
            raise ValueError("only a limited search records the managed directory as searched")
        located = self.state is ResolutionState.LOCATED
        private = (
            self.ffmpeg_path,
            self.ffprobe_path,
            self.scratch_root,
            self.ffmpeg_identity,
            self.ffprobe_identity,
        )
        if located != all(value is not None for value in private):
            raise ValueError("only a located resolution carries a pair")
        if not located and any(value is not None for value in private):
            raise ValueError("only a located resolution carries a pair")
        if located and self.source_kind is SourceKind.NONE:
            raise ValueError("a located resolution names its source")

    def __repr__(self) -> str:
        return (
            f"<MediaRuntimeResolution state={self.state.value} reason={self.reason.value} "
            f"source={self.source_kind.value}>"
        )

    @property
    def install_resolves(self) -> bool:
        """Whether installing the managed profile is the setup that makes this resolution usable.

        IMPORTANT: true for an exhaustive `supported_pair_missing` and for a `discovery_limit`
        whose search examined the managed directory. A real Windows PATH routinely holds more
        directories than the candidate bound (the supplied host: 44 eligible), so treating only
        the exhaustive reason as installable left missing tools with no install offer at all.
        Do not widen this to a timeout, a worker failure or a limit reached before enumeration:
        those never examined the managed directory, which may hold a valid runtime that
        publication would retire.
        """

        return self.state is ResolutionState.UNAVAILABLE and (
            self.reason is ResolutionReason.SUPPORTED_PAIR_MISSING or self.managed_searched
        )

    def to_wire(self) -> dict[str, str]:
        """The locator-free projection; it never claims media or render readiness."""

        return {
            "schema": RESOLUTION_SCHEMA,
            "state": self.state.value,
            "reason": self.reason.value,
            "source_kind": self.source_kind.value,
        }


def _unavailable(
    reason: ResolutionReason, source: SourceKind = SourceKind.NONE
) -> MediaRuntimeResolution:
    return MediaRuntimeResolution(ResolutionState.UNAVAILABLE, reason, source)


def _invalid(reason: ResolutionReason, source: SourceKind) -> MediaRuntimeResolution:
    return MediaRuntimeResolution(ResolutionState.INVALID_CONFIG, reason, source)


_DISCOVERING = MediaRuntimeResolution(
    ResolutionState.DISCOVERING, ResolutionReason.DISCOVERY_IN_PROGRESS
)


# --------------------------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeInputs:
    """The process facts one resolution depends on; also the cache identity."""

    platform: str
    machine: str
    pointer_bits: int
    python_prefix: str
    path: str | None
    legacy: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        return f"<RuntimeInputs platform={self.platform} legacy={len(self.legacy)}>"


def _environment_value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    if value is not None:
        return value
    folded = name.casefold()
    for key, candidate in environment.items():
        if key.casefold() == folded:
            return candidate
    return None


def capture_runtime_inputs(environment: Mapping[str, str] | None = None) -> RuntimeInputs:
    values: Mapping[str, str] = os.environ if environment is None else environment
    legacy = tuple(
        (name, value)
        for name in LEGACY_NAMES
        if (value := _environment_value(values, name)) is not None
    )
    return RuntimeInputs(
        platform=sys.platform,
        machine=platform.machine(),
        pointer_bits=struct.calcsize("P") * 8,
        python_prefix=sys.prefix,
        path=_environment_value(values, "PATH"),
        legacy=legacy,
    )


def _supported_platform(inputs: RuntimeInputs) -> bool:
    return (
        inputs.platform == "win32"
        and inputs.machine.casefold() in _SUPPORTED_MACHINES
        and inputs.pointer_bits == 64
    )


# --------------------------------------------------------------------------------------------
# Legacy normalization


class LegacyKind(str, Enum):
    AUTO = "auto"
    EXPLICIT = "explicit"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, repr=False)
class LegacyNormalization:
    kind: LegacyKind
    reason: ResolutionReason | None = None
    directory: PureWindowsPath | None = None
    scratch: PureWindowsPath | None = None

    def __repr__(self) -> str:
        return f"<LegacyNormalization kind={self.kind.value}>"


def normalize_legacy_environment(values: Mapping[str, str]) -> LegacyNormalization:
    """Translate the five legacy environment values into the new resolver's precedence.

    All absent, or a positive marker alone, is automatic discovery. A complete same-directory pair
    is an explicit selection with an optional scratch. Anything partial, blank, split or with an
    unrecognized marker is a typed configuration error with no fallback and no invented meaning.
    """

    def invalid(reason: ResolutionReason) -> LegacyNormalization:
        return LegacyNormalization(LegacyKind.INVALID, reason)

    for marker in (LEGACY_MEDIA_MARKER, LEGACY_RENDER_MARKER):
        value = values.get(marker)
        # IMPORTANT: "0" never meant "disabled"; the legacy activation rejected it as invalid.
        # Reading it as an opt-out now would invent a semantics no configuration ever had.
        if value is not None and value != "1":
            return invalid(ResolutionReason.OVERRIDE_INVALID_MARKER)
    ffmpeg = values.get(LEGACY_FFMPEG)
    ffprobe = values.get(LEGACY_FFPROBE)
    scratch = values.get(LEGACY_SCRATCH)
    present = [value for value in (ffmpeg, ffprobe, scratch) if value is not None]
    if not present:
        return LegacyNormalization(LegacyKind.AUTO)
    if any(not value.strip() for value in present):
        return invalid(ResolutionReason.OVERRIDE_INCOMPLETE)
    if ffmpeg is None or ffprobe is None:
        return invalid(ResolutionReason.OVERRIDE_INCOMPLETE)
    if len(ffmpeg) > MAX_LOCATOR_CHARS or len(ffprobe) > MAX_LOCATOR_CHARS:
        return invalid(ResolutionReason.OVERRIDE_INVALID_PATH)
    ffmpeg_parent, ffmpeg_name = ntpath.split(ffmpeg)
    ffprobe_parent, ffprobe_name = ntpath.split(ffprobe)
    if ffmpeg_name.casefold() != FFMPEG_NAME or ffprobe_name.casefold() != FFPROBE_NAME:
        return invalid(ResolutionReason.OVERRIDE_INVALID_PATH)
    ffmpeg_directory = lexical_local_directory(ffmpeg_parent)
    ffprobe_directory = lexical_local_directory(ffprobe_parent)
    if ffmpeg_directory is None or ffprobe_directory is None:
        return invalid(ResolutionReason.OVERRIDE_INVALID_PATH)
    if locator_identity(ffmpeg_directory) != locator_identity(ffprobe_directory):
        return invalid(ResolutionReason.OVERRIDE_SPLIT_DIRECTORY)
    scratch_locator = None
    if scratch is not None:
        scratch_locator = lexical_local_directory(scratch)
        if scratch_locator is None:
            return invalid(ResolutionReason.PRIVATE_ROOT_INVALID)
    return LegacyNormalization(
        LegacyKind.EXPLICIT, directory=ffmpeg_directory, scratch=scratch_locator
    )


# --------------------------------------------------------------------------------------------
# Private configuration


class MediaRuntimeSelection(str, Enum):
    AUTO = "auto"
    LOCAL = "local"


@dataclass(frozen=True, slots=True, repr=False)
class MediaRuntimeConfig:
    revision: int
    selection: MediaRuntimeSelection
    directory: str | None

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision")
        if not isinstance(self.selection, MediaRuntimeSelection):
            raise ValueError("selection")
        if self.selection is MediaRuntimeSelection.AUTO:
            if self.directory is not None:
                raise ValueError("directory")
        elif lexical_local_directory(self.directory) is None:
            raise ValueError("directory")

    def __repr__(self) -> str:
        return f"<MediaRuntimeConfig revision={self.revision} selection={self.selection.value}>"


class MediaRuntimeConfigError(RuntimeError):
    """Closed, content-free configuration failure."""

    CODES = frozenset(
        {
            "config_corrupt",
            "config_unsupported",
            "config_conflict",
            "config_write_failed",
            "invalid_request",
            "private_root_invalid",
            "unsupported_host",
            "unsupported_platform",
            "selection_invalid_path",
            "selection_unsupported_pair",
            "discovery_timeout",
            "discovery_limit",
            "worker_failure",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown configuration error code")
        self.code = code
        super().__init__(code)


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite constant")


def parse_config(payload: bytes) -> MediaRuntimeConfig:
    """Strictly decode config v1; malformed input is corrupt, another schema is unsupported."""

    if type(payload) is not bytes or not 0 < len(payload) <= MAX_CONFIG_BYTES:
        raise MediaRuntimeConfigError("config_corrupt")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MediaRuntimeConfigError("config_corrupt") from exc
    if type(value) is not dict:
        raise MediaRuntimeConfigError("config_corrupt")
    schema = value.get("schema")
    if type(schema) is not str:
        raise MediaRuntimeConfigError("config_corrupt")
    if schema != CONFIG_SCHEMA:
        raise MediaRuntimeConfigError("config_unsupported")
    if set(value) != {"schema", "revision", "selection", "directory"}:
        raise MediaRuntimeConfigError("config_corrupt")
    directory = value["directory"]
    if directory is not None and (type(directory) is not str or len(directory) > MAX_LOCATOR_CHARS):
        raise MediaRuntimeConfigError("config_corrupt")
    try:
        return MediaRuntimeConfig(
            revision=value["revision"],
            selection=MediaRuntimeSelection(value["selection"]),
            directory=directory,
        )
    except (TypeError, ValueError) as exc:
        raise MediaRuntimeConfigError("config_corrupt") from exc


def encode_config(config: MediaRuntimeConfig) -> bytes:
    payload = json.dumps(
        {
            "schema": CONFIG_SCHEMA,
            "revision": config.revision,
            "selection": config.selection.value,
            "directory": config.directory,
        },
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise MediaRuntimeConfigError("invalid_request")
    return payload + b"\n"


@dataclass(frozen=True, slots=True, repr=False)
class MediaRuntimePrivateLayout:
    """The fixed H3-owned children of the host private root. Code-owned, never client input."""

    root: Path
    media_runtime: Path
    config_file: Path
    runtime_root: Path
    managed_bin: Path
    scratch_root: Path

    def __repr__(self) -> str:
        return "<MediaRuntimePrivateLayout opaque>"


def private_layout(
    root: Path, profile_component: str = MANAGED_PROFILE_COMPONENT
) -> MediaRuntimePrivateLayout:
    media_runtime = root / _MEDIA_RUNTIME_DIRECTORY
    runtime_root = media_runtime / "runtime"
    return MediaRuntimePrivateLayout(
        root=root,
        media_runtime=media_runtime,
        config_file=media_runtime / _CONFIG_FILENAME,
        runtime_root=runtime_root,
        managed_bin=runtime_root / profile_component / "bin",
        scratch_root=media_runtime / "scratch",
    )


# --------------------------------------------------------------------------------------------
# Host storage port


class HostRootError(RuntimeError):
    def __init__(self, reason: ResolutionReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class HostRootPort(Protocol):
    def private_root(self) -> Path: ...

    def served_roots(self) -> tuple[Path, ...]: ...


def _host_locator(value: object) -> Path:
    locator = lexical_local_directory(value)
    if locator is None:
        raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)
    return Path(str(locator))


def _loaded_folder_paths() -> object | None:
    return sys.modules.get("folder_paths")


class ComfyHostRootPort:
    """Reads ComfyUI's `folder_paths` lazily; importing this module touches no host."""

    def __init__(self, *, module_provider: Callable[[], object | None] = _loaded_folder_paths):
        self._module_provider = module_provider

    def __repr__(self) -> str:
        return "<ComfyHostRootPort>"

    def _folder_paths(self) -> object:
        module = self._module_provider()
        if module is None:
            raise HostRootError(ResolutionReason.UNSUPPORTED_HOST)
        return module

    def private_root(self) -> Path:
        factory = getattr(self._folder_paths(), "get_system_user_directory", None)
        if not callable(factory):
            raise HostRootError(ResolutionReason.UNSUPPORTED_HOST)
        try:
            value = factory(PRIVATE_ROOT_NAME)
        except Exception as exc:
            raise HostRootError(ResolutionReason.UNSUPPORTED_HOST) from exc
        return _host_locator(value)

    def served_roots(self) -> tuple[Path, ...]:
        module = self._folder_paths()
        roots: list[Path] = []
        for name in ("get_input_directory", "get_output_directory", "get_temp_directory"):
            factory = getattr(module, name, None)
            if not callable(factory):
                raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)
            try:
                value = factory()
            except Exception as exc:
                raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID) from exc
            if type(value) is not str:
                raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)
            roots.append(_host_locator(ntpath.normpath(value)))
        return tuple(roots)


def _link_free_existing_prefix(path: Path) -> bool:
    """Every existing component is a real directory; a missing tail is allowed (never created)."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            current.is_symlink()
            or stat.S_ISLNK(metadata.st_mode)
            or attributes & reparse
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            return False
    return True


def _overlaps(left: Path, right: Path) -> bool:
    a = locator_identity(PureWindowsPath(str(left)))
    b = locator_identity(PureWindowsPath(str(right)))
    return a == b or a.startswith(b + "\\") or b.startswith(a + "\\")


# --------------------------------------------------------------------------------------------
# Supervised worker


class DiscoveryWorkerError(RuntimeError):
    CODES = frozenset({"discovery_timeout", "discovery_cancelled", "worker_failure"})

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown worker error code")
        self.code = code
        super().__init__(code)


class DiscoveryWorkerPort(Protocol):
    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse: ...

    def cancel(self) -> None: ...


# CRITICAL: the worker must never execute `comfyui_h3_context/__init__.py`. Package import registers
# host routes and runs the legacy explicit media activation from the environment, so a worker that
# imported the package normally could construct and publish a media adapter as a side effect of a
# discovery query. The bootstrap installs empty namespace stand-ins for the three packages and
# imports only the worker module and the pure admission code it names.
_WORKER_BOOTSTRAP = (
    "import os, sys, types\n"
    "root = sys.argv[1]\n"
    "sys.path.insert(0, root)\n"
    "for name in ('comfyui_h3_context', 'comfyui_h3_context.core', "
    "'comfyui_h3_context.adapters'):\n"
    "    module = types.ModuleType(name)\n"
    "    module.__path__ = [os.path.join(root, *name.split('.'))]\n"
    "    sys.modules[name] = module\n"
    "from comfyui_h3_context.adapters.media_runtime_discovery_worker import main\n"
    "raise SystemExit(main())\n"
)


def _package_parent() -> Path:
    return Path(__file__).resolve().parents[2]


def _worker_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


class SubprocessDiscoveryWorker:
    """One short-lived isolated interpreter per resolution; at most one alive per process."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        package_parent: Path | None = None,
    ) -> None:
        self._python = python_executable or sys.executable
        self._package_parent = package_parent or _package_parent()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._stuck: subprocess.Popen[bytes] | None = None
        self._cancelled = False

    def __repr__(self) -> str:
        return "<SubprocessDiscoveryWorker>"

    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse:
        payload = encode_request(request)
        with self._lock:
            # CRITICAL: a worker that survived its kill blocks every later discovery until it is
            # reaped. Spawning past it is how an unkillable filesystem call becomes an unbounded
            # pile of blocked interpreters.
            if self._stuck is not None:
                if self._stuck.poll() is None:
                    raise DiscoveryWorkerError("worker_failure")
                self._stuck = None
            if self._process is not None:
                raise DiscoveryWorkerError("worker_failure")
            self._cancelled = False
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed argv, isolated interpreter
                    [
                        self._python,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        _WORKER_BOOTSTRAP,
                        str(self._package_parent),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=str(self._package_parent),
                    env=_worker_environment(),
                    shell=False,
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, ValueError) as exc:
                raise DiscoveryWorkerError("worker_failure") from exc
            self._process = process
        output: list[bytes] = []

        def write() -> None:
            stdin = process.stdin
            try:
                if stdin is not None:
                    stdin.write(payload)
            except OSError:
                pass
            finally:
                if stdin is not None:
                    try:
                        stdin.close()
                    except OSError:
                        pass

        def read() -> None:
            stdout = process.stdout
            try:
                if stdout is not None:
                    output.append(stdout.read(MAX_RESPONSE_BYTES + 1))
            except OSError:
                pass

        writer = threading.Thread(target=write, name="h3-media-discovery-write", daemon=True)
        reader = threading.Thread(target=read, name="h3-media-discovery-read", daemon=True)
        writer.start()
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=max(0.0, timeout_seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
        stuck = False
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=WORKER_REAP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                stuck = process.poll() is None
        writer.join(WORKER_REAP_SECONDS)
        reader.join(WORKER_REAP_SECONDS)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        with self._lock:
            self._process = None
            cancelled = self._cancelled
            self._cancelled = False
            if stuck:
                self._stuck = process
        if stuck:
            raise DiscoveryWorkerError("worker_failure")
        if cancelled:
            raise DiscoveryWorkerError("discovery_cancelled")
        if timed_out:
            raise DiscoveryWorkerError("discovery_timeout")
        body = output[0] if output else b""
        if process.returncode != 0 or len(body) > MAX_RESPONSE_BYTES:
            raise DiscoveryWorkerError("worker_failure")
        try:
            return decode_response(body)
        except DiscoveryProtocolError as exc:
            raise DiscoveryWorkerError("worker_failure") from exc

    def cancel(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            self._cancelled = True
        try:
            process.kill()
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# Resolver


@dataclass(slots=True)
class _Inflight:
    key: object
    event: threading.Event
    superseded: bool = False
    result: MediaRuntimeResolution | None = None


@dataclass(frozen=True, slots=True)
class _Cached:
    key: object
    result: MediaRuntimeResolution
    expires_at: float


@dataclass(frozen=True, slots=True)
class _ConfigSnapshot:
    identity: FileIdentity | None
    config: MediaRuntimeConfig | None
    error: ResolutionReason | None


class MediaRuntimeResolver:
    """Process service; construction is inert and performs no filesystem or process work."""

    def __init__(
        self,
        *,
        host_roots: HostRootPort,
        worker: DiscoveryWorkerPort,
        package_parent: Path | None = None,
        inputs_provider: Callable[[], RuntimeInputs] = capture_runtime_inputs,
        clock: Callable[[], float] = time.monotonic,
        ffmpeg_sha256: str | None = None,
        ffprobe_sha256: str | None = None,
        profile_component: str = MANAGED_PROFILE_COMPONENT,
        deadline_seconds: float = RESOLUTION_DEADLINE_SECONDS,
    ) -> None:
        capability = qualified_ffmpeg_capability()
        self._host_roots = host_roots
        self._worker = worker
        self._package_parent = package_parent or _package_parent()
        self._inputs_provider = inputs_provider
        self._clock = clock
        self._ffmpeg_sha256 = ffmpeg_sha256 or capability.ffmpeg_sha256
        self._ffprobe_sha256 = ffprobe_sha256 or capability.ffprobe_sha256
        self._profile_component = profile_component
        self._deadline_seconds = deadline_seconds
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._inflight: _Inflight | None = None
        self._cached: _Cached | None = None

    def __repr__(self) -> str:
        return "<MediaRuntimeResolver>"

    # -- queries ---------------------------------------------------------------------------

    def peek(self) -> MediaRuntimeResolution | None:
        """The current unexpired result or an in-progress marker, without any I/O."""

        with self._state_lock:
            cached = self._cached
            if cached is not None and self._clock() < cached.expires_at:
                return cached.result
            if self._inflight is not None:
                return _DISCOVERING
            return None

    def invalidate(self) -> None:
        """Drop the cached result and supersede any discovery in flight."""

        with self._state_lock:
            self._cached = None
            inflight = self._inflight
            if inflight is not None:
                inflight.superseded = True
        if inflight is not None:
            self._worker.cancel()

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        """Resolve the pair. Blocking: call from a worker thread, never the host event loop."""

        deadline = self._clock() + self._deadline_seconds
        inputs = self._inputs_provider()
        if not _supported_platform(inputs):
            # Other platforms are unsupported without probing a single executable.
            return _unavailable(ResolutionReason.UNSUPPORTED_PLATFORM)
        root, root_reason = self._private_root()
        snapshot = (
            _ConfigSnapshot(None, None, None)
            if root is None
            else self._config_snapshot(private_layout(root, self._profile_component))
        )
        key = (
            self._profile_component,
            self._ffmpeg_sha256,
            self._ffprobe_sha256,
            inputs,
            None if root is None else str(root),
            root_reason,
            snapshot.identity,
            snapshot.config,
            snapshot.error,
        )
        superseded: _Inflight | None = None
        with self._state_lock:
            cached = self._cached
            if rescan:
                self._cached = None
            elif cached is not None and cached.key == key and self._clock() < cached.expires_at:
                return cached.result
            inflight = self._inflight
            if inflight is not None and inflight.key == key and not inflight.superseded:
                mine = inflight
                owner = False
            else:
                if inflight is not None:
                    inflight.superseded = True
                    superseded = inflight
                mine = _Inflight(key=key, event=threading.Event())
                self._inflight = mine
                owner = True
        if not owner:
            mine.event.wait(max(0.0, deadline - self._clock()) + WORKER_REAP_SECONDS)
            return mine.result if mine.result is not None else _DISCOVERING
        if superseded is not None:
            # IMPORTANT: changed inputs replace the old request rather than queueing behind it.
            # The old worker is killed so the one-worker bound holds; its caller reports
            # `discovering` because a newer resolution now owns currency.
            self._worker.cancel()
        result = _DISCOVERING
        acquired = self._run_lock.acquire(timeout=max(0.0, deadline - self._clock()))
        try:
            if not acquired:
                result = _unavailable(ResolutionReason.DISCOVERY_TIMEOUT)
            elif not mine.superseded:
                result = self._discover(inputs, root, root_reason, snapshot, deadline)
        finally:
            if acquired:
                self._run_lock.release()
            with self._state_lock:
                if self._inflight is mine:
                    self._inflight = None
                if mine.superseded:
                    # CRITICAL: a late result for an invalidated snapshot must never become
                    # current. Publishing it would let a rescan, a config write or an install
                    # be silently overwritten by the discovery it was meant to replace.
                    result = _DISCOVERING
                else:
                    lifetime = (
                        POSITIVE_CACHE_SECONDS
                        if result.state is ResolutionState.LOCATED
                        else NEGATIVE_CACHE_SECONDS
                    )
                    self._cached = _Cached(key, result, self._clock() + lifetime)
                mine.result = result
                mine.event.set()
        return result

    # -- configuration ---------------------------------------------------------------------

    def read_config(self) -> MediaRuntimeConfig | None:
        """The persisted selection, `None` when absent. Corrupt files raise and stay untouched."""

        if not _supported_platform(self._inputs_provider()):
            raise MediaRuntimeConfigError("unsupported_platform")
        root, reason = self._private_root()
        if root is None:
            raise MediaRuntimeConfigError(
                "unsupported_host"
                if reason is ResolutionReason.UNSUPPORTED_HOST
                else "private_root_invalid"
            )
        snapshot = self._config_snapshot(private_layout(root, self._profile_component))
        if snapshot.error is ResolutionReason.PRIVATE_ROOT_INVALID:
            raise MediaRuntimeConfigError("private_root_invalid")
        if snapshot.error is ResolutionReason.CONFIG_UNSUPPORTED:
            raise MediaRuntimeConfigError("config_unsupported")
        if snapshot.error is not None:
            raise MediaRuntimeConfigError("config_corrupt")
        return snapshot.config

    def write_config(
        self,
        *,
        expected_revision: int,
        selection: MediaRuntimeSelection,
        directory: str | None = None,
    ) -> MediaRuntimeConfig:
        """Validate, then atomically replace config v1 if it is still at `expected_revision`.

        Expected revision zero means "no config file". Every failure leaves the previous bytes
        and the active selection in place.
        """

        if type(expected_revision) is not int or expected_revision < 0:
            raise MediaRuntimeConfigError("invalid_request")
        if not isinstance(selection, MediaRuntimeSelection):
            raise MediaRuntimeConfigError("invalid_request")
        if selection is MediaRuntimeSelection.AUTO and directory is not None:
            raise MediaRuntimeConfigError("invalid_request")
        normalized: str | None = None
        if selection is MediaRuntimeSelection.LOCAL:
            locator = lexical_local_directory(directory)
            if locator is None:
                raise MediaRuntimeConfigError("selection_invalid_path")
            normalized = str(locator)
        with self._config_lock:
            if not _supported_platform(self._inputs_provider()):
                raise MediaRuntimeConfigError("unsupported_platform")
            root, reason = self._private_root()
            if root is None:
                raise MediaRuntimeConfigError(
                    "unsupported_host"
                    if reason is ResolutionReason.UNSUPPORTED_HOST
                    else "private_root_invalid"
                )
            layout = private_layout(root, self._profile_component)
            current = self._config_snapshot(layout)
            if current.error is ResolutionReason.PRIVATE_ROOT_INVALID:
                raise MediaRuntimeConfigError("private_root_invalid")
            if current.error is ResolutionReason.CONFIG_UNSUPPORTED:
                raise MediaRuntimeConfigError("config_unsupported")
            if current.error is not None:
                raise MediaRuntimeConfigError("config_corrupt")
            current_revision = 0 if current.config is None else current.config.revision
            if current_revision != expected_revision:
                raise MediaRuntimeConfigError("config_conflict")
            if normalized is not None:
                self._admit_selection(normalized)
            updated = MediaRuntimeConfig(current_revision + 1, selection, normalized)
            self._commit_config(layout, current.identity, encode_config(updated))
        self.invalidate()
        return updated

    def private_layout(self) -> MediaRuntimePrivateLayout:
        """The validated private layout for successor setup code; raises when unavailable."""

        root, reason = self._private_root()
        if root is None:
            raise MediaRuntimeConfigError(
                "unsupported_host"
                if reason is ResolutionReason.UNSUPPORTED_HOST
                else "private_root_invalid"
            )
        return private_layout(root, self._profile_component)

    # -- internals -------------------------------------------------------------------------

    def _private_root(self) -> tuple[Path | None, ResolutionReason | None]:
        try:
            root = self._host_roots.private_root()
        except HostRootError as exc:
            return None, exc.reason
        if not isinstance(root, Path) or lexical_local_directory(str(root)) is None:
            return None, ResolutionReason.PRIVATE_ROOT_INVALID
        return root, None

    def _config_snapshot(self, layout: MediaRuntimePrivateLayout) -> _ConfigSnapshot:
        if not _link_free_existing_prefix(layout.media_runtime):
            return _ConfigSnapshot(None, None, ResolutionReason.PRIVATE_ROOT_INVALID)
        for _attempt in range(2):
            try:
                before = layout.config_file.lstat()
            except FileNotFoundError:
                return _ConfigSnapshot(None, None, None)
            except OSError:
                return _ConfigSnapshot(None, None, ResolutionReason.CONFIG_CORRUPT)
            identity = _identity(before)
            if (
                not stat.S_ISREG(before.st_mode)
                or layout.config_file.is_symlink()
                or getattr(before, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or before.st_nlink != 1
                or before.st_size > MAX_CONFIG_BYTES
            ):
                return _ConfigSnapshot(identity, None, ResolutionReason.CONFIG_CORRUPT)
            try:
                payload = read_regular_file_bytes(
                    layout.config_file, maximum_bytes=MAX_CONFIG_BYTES
                )
                after = layout.config_file.lstat()
            except (UnsafePathError, OSError):
                return _ConfigSnapshot(identity, None, ResolutionReason.CONFIG_CORRUPT)
            if _identity(after) != identity:
                continue
            try:
                return _ConfigSnapshot(identity, parse_config(payload), None)
            except MediaRuntimeConfigError as exc:
                reason = (
                    ResolutionReason.CONFIG_UNSUPPORTED
                    if exc.code == "config_unsupported"
                    else ResolutionReason.CONFIG_CORRUPT
                )
                return _ConfigSnapshot(identity, None, reason)
        return _ConfigSnapshot(None, None, ResolutionReason.CONFIG_CORRUPT)

    def _commit_config(
        self,
        layout: MediaRuntimePrivateLayout,
        expected_identity: FileIdentity | None,
        payload: bytes,
    ) -> None:
        try:
            parent = ensure_directory(layout.media_runtime)
        except UnsafePathError as exc:
            raise MediaRuntimeConfigError("private_root_invalid") from exc
        temporary = parent / f"{_CONFIG_FILENAME}.{secrets.token_hex(12)}.tmp"
        created = False
        try:
            try:
                _write_new_file(temporary, payload)
            except ArtifactStoreError as exc:
                raise MediaRuntimeConfigError("config_write_failed") from exc
            created = True
            with _validated_directories(parent):
                # CRITICAL: compare-and-swap at the commit point, not only at the start. A second
                # writer between validation and replacement would otherwise be silently overwritten
                # by a revision computed from bytes that are no longer on disk.
                current = self._config_snapshot(layout)
                if current.identity != expected_identity or current.error is not None:
                    raise MediaRuntimeConfigError("config_conflict")
                _replace_file(temporary, layout.config_file)
            created = False
        except MediaRuntimeConfigError:
            raise
        except (ArtifactStoreError, OSError) as exc:
            raise MediaRuntimeConfigError("config_write_failed") from exc
        finally:
            if created:
                # Only the uniquely named temporary this call created is ever removed.
                try:
                    _safe_unlink_pinned(temporary, missing_ok=True, maximum_links=1)
                except ArtifactStoreError:
                    pass

    def _admit_selection(self, directory: str) -> None:
        request = DiscoveryRequest(
            mode=DiscoveryMode.EXPLICIT,
            candidates=(directory,),
            ffmpeg_sha256=self._ffmpeg_sha256,
            ffprobe_sha256=self._ffprobe_sha256,
            budget_ms=int(self._deadline_seconds * 1000),
        )
        with self._run_lock:
            try:
                response = self._worker.run(request, timeout_seconds=self._deadline_seconds)
            except DiscoveryWorkerError as exc:
                code = "discovery_timeout" if exc.code == "discovery_timeout" else "worker_failure"
                raise MediaRuntimeConfigError(code) from exc
        outcome = response.outcome
        if outcome is DiscoveryOutcome.ADMITTED:
            return
        if outcome is DiscoveryOutcome.UNSAFE_PATH:
            raise MediaRuntimeConfigError("selection_invalid_path")
        if outcome is DiscoveryOutcome.UNSUPPORTED_PAIR:
            raise MediaRuntimeConfigError("selection_unsupported_pair")
        if outcome is DiscoveryOutcome.TIMEOUT:
            raise MediaRuntimeConfigError("discovery_timeout")
        if outcome is DiscoveryOutcome.LIMIT:
            raise MediaRuntimeConfigError("discovery_limit")
        raise MediaRuntimeConfigError("worker_failure")

    def _run_worker(
        self,
        request_candidates: Sequence[str],
        mode: DiscoveryMode,
        scratch: str | None,
        deadline: float,
    ) -> DiscoveryResponse | ResolutionReason:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return ResolutionReason.DISCOVERY_TIMEOUT
        request = DiscoveryRequest(
            mode=mode,
            candidates=tuple(request_candidates),
            ffmpeg_sha256=self._ffmpeg_sha256,
            ffprobe_sha256=self._ffprobe_sha256,
            budget_ms=max(1, int(remaining * 1000)),
            scratch=scratch,
        )
        try:
            return self._worker.run(request, timeout_seconds=remaining)
        except DiscoveryWorkerError as exc:
            if exc.code == "worker_failure":
                return ResolutionReason.WORKER_FAILURE
            return ResolutionReason.DISCOVERY_TIMEOUT

    def _located(
        self,
        response: DiscoveryResponse,
        directory: str,
        scratch: Path,
        source: SourceKind,
    ) -> MediaRuntimeResolution:
        base = Path(directory)
        # IMPORTANT: a located result is a locator plus the identities seen at discovery, not a
        # capability. Every consumer must repeat the exact pin/hash across its own spawn; treating
        # this cached value as proof would let a same-named file replaced after discovery run.
        return MediaRuntimeResolution(
            ResolutionState.LOCATED,
            ResolutionReason.PAIR_ADMITTED,
            source,
            ffmpeg_path=base / FFMPEG_NAME,
            ffprobe_path=base / FFPROBE_NAME,
            scratch_root=scratch,
            ffmpeg_identity=response.ffmpeg_identity,
            ffprobe_identity=response.ffprobe_identity,
            profile=self._profile_component,
        )

    def _selected(
        self,
        directory: str,
        scratch: Path,
        explicit_scratch: str | None,
        source: SourceKind,
        deadline: float,
    ) -> MediaRuntimeResolution:
        explicit = source is SourceKind.EXPLICIT_OVERRIDE
        # CRITICAL: an explicit or persisted selection is authoritative. Its failure is reported
        # as its own error and never followed by automatic discovery: silently using some other
        # FFmpeg would hide an administrator's mistake behind a result that looks healthy.
        outcome = self._run_worker((directory,), DiscoveryMode.EXPLICIT, explicit_scratch, deadline)
        if isinstance(outcome, ResolutionReason):
            return _unavailable(outcome, source)
        if outcome.outcome is DiscoveryOutcome.ADMITTED:
            return self._located(outcome, directory, scratch, source)
        if outcome.outcome is DiscoveryOutcome.UNSAFE_PATH:
            reason = (
                ResolutionReason.OVERRIDE_INVALID_PATH
                if explicit
                else ResolutionReason.LOCAL_SELECTION_INVALID_PATH
            )
            return _invalid(reason, source)
        if outcome.outcome is DiscoveryOutcome.UNSUPPORTED_PAIR:
            reason = (
                ResolutionReason.OVERRIDE_UNSUPPORTED_PAIR
                if explicit
                else ResolutionReason.LOCAL_SELECTION_UNSUPPORTED_PAIR
            )
            return _invalid(reason, source)
        if outcome.outcome is DiscoveryOutcome.SCRATCH_UNSAFE:
            return _invalid(ResolutionReason.PRIVATE_ROOT_INVALID, source)
        if outcome.outcome is DiscoveryOutcome.TIMEOUT:
            return _unavailable(ResolutionReason.DISCOVERY_TIMEOUT, source)
        if outcome.outcome is DiscoveryOutcome.LIMIT:
            return _unavailable(ResolutionReason.DISCOVERY_LIMIT, source)
        return _unavailable(ResolutionReason.WORKER_FAILURE, source)

    def _explicit_scratch(self, scratch: PureWindowsPath) -> Path | None:
        candidate = Path(str(scratch))
        try:
            served = self._host_roots.served_roots()
        except HostRootError:
            return None
        if _overlaps(candidate, self._package_parent) or any(
            _overlaps(candidate, root) for root in served
        ):
            return None
        return candidate

    def _auto_candidates(
        self, inputs: RuntimeInputs, layout: MediaRuntimePrivateLayout
    ) -> tuple[list[tuple[str, SourceKind]], bool] | None:
        if inputs.path is not None and len(inputs.path.encode("utf-8", "surrogatepass")) > (
            MAX_PATH_SNAPSHOT_BYTES
        ):
            return None
        ordered: list[tuple[str, SourceKind]] = [
            (str(layout.managed_bin), SourceKind.MANAGED),
            (ntpath.join(inputs.python_prefix, "Library", "bin"), SourceKind.PYTHON_PREFIX),
            (ntpath.join(inputs.python_prefix, "bin"), SourceKind.PYTHON_PREFIX),
        ]
        if inputs.path:
            ordered.extend((entry, SourceKind.SYSTEM_PATH) for entry in inputs.path.split(";"))
        seen: set[str] = set()
        admitted: list[tuple[str, SourceKind]] = []
        truncated = False
        for raw, source in ordered:
            # Relative, empty, CWD, UNC, device, quoted and unexpanded-variable entries are
            # ineligible by construction; `shutil.which`-style resolution is never used.
            locator = lexical_local_directory(raw)
            if locator is None:
                continue
            identity = locator_identity(locator)
            if identity in seen:
                continue
            if len(admitted) >= MAX_CANDIDATE_DIRECTORIES:
                truncated = True
                break
            seen.add(identity)
            admitted.append((str(locator), source))
        return admitted, truncated

    def _discover(
        self,
        inputs: RuntimeInputs,
        root: Path | None,
        root_reason: ResolutionReason | None,
        snapshot: _ConfigSnapshot,
        deadline: float,
    ) -> MediaRuntimeResolution:
        legacy = normalize_legacy_environment(dict(inputs.legacy))
        if legacy.kind is LegacyKind.INVALID or (
            legacy.kind is LegacyKind.EXPLICIT and legacy.directory is None
        ):
            return _invalid(
                legacy.reason or ResolutionReason.OVERRIDE_INCOMPLETE, SourceKind.EXPLICIT_OVERRIDE
            )
        if legacy.kind is LegacyKind.EXPLICIT and legacy.directory is not None:
            if legacy.scratch is not None:
                scratch = self._explicit_scratch(legacy.scratch)
                if scratch is None:
                    return _invalid(
                        ResolutionReason.PRIVATE_ROOT_INVALID, SourceKind.EXPLICIT_OVERRIDE
                    )
                return self._selected(
                    str(legacy.directory),
                    scratch,
                    str(scratch),
                    SourceKind.EXPLICIT_OVERRIDE,
                    deadline,
                )
            if root is None:
                return _unavailable(
                    root_reason or ResolutionReason.UNSUPPORTED_HOST, SourceKind.EXPLICIT_OVERRIDE
                )
            layout = private_layout(root, self._profile_component)
            return self._selected(
                str(legacy.directory),
                layout.scratch_root,
                None,
                SourceKind.EXPLICIT_OVERRIDE,
                deadline,
            )
        if root is None:
            return _unavailable(root_reason or ResolutionReason.UNSUPPORTED_HOST)
        layout = private_layout(root, self._profile_component)
        if snapshot.error is ResolutionReason.PRIVATE_ROOT_INVALID:
            return _unavailable(ResolutionReason.PRIVATE_ROOT_INVALID)
        if snapshot.error is not None:
            return _invalid(snapshot.error, SourceKind.LOCAL_SELECTION)
        config = snapshot.config
        if config is not None and config.selection is MediaRuntimeSelection.LOCAL:
            if config.directory is None:
                return _invalid(ResolutionReason.CONFIG_CORRUPT, SourceKind.LOCAL_SELECTION)
            return self._selected(
                config.directory, layout.scratch_root, None, SourceKind.LOCAL_SELECTION, deadline
            )
        candidates = self._auto_candidates(inputs, layout)
        if candidates is None:
            return _unavailable(ResolutionReason.DISCOVERY_LIMIT)
        ordered, truncated = candidates
        if not ordered:
            return _unavailable(ResolutionReason.SUPPORTED_PAIR_MISSING)
        outcome = self._run_worker(
            [directory for directory, _source in ordered], DiscoveryMode.AUTO, None, deadline
        )
        if isinstance(outcome, ResolutionReason):
            return _unavailable(outcome)
        if outcome.outcome is DiscoveryOutcome.ADMITTED:
            if outcome.index is None or outcome.index >= len(ordered):
                return _unavailable(ResolutionReason.WORKER_FAILURE)
            directory, source = ordered[outcome.index]
            return self._located(outcome, directory, layout.scratch_root, source)
        if outcome.outcome is DiscoveryOutcome.EXHAUSTED:
            # A truncated search never claims the supported pair is absent, but it records that
            # the managed directory (always first) was among the candidates it exhausted.
            if truncated:
                return MediaRuntimeResolution(
                    ResolutionState.UNAVAILABLE,
                    ResolutionReason.DISCOVERY_LIMIT,
                    managed_searched=any(source is SourceKind.MANAGED for _, source in ordered),
                )
            return _unavailable(ResolutionReason.SUPPORTED_PAIR_MISSING)
        if outcome.outcome is DiscoveryOutcome.LIMIT:
            return _unavailable(ResolutionReason.DISCOVERY_LIMIT)
        if outcome.outcome is DiscoveryOutcome.TIMEOUT:
            return _unavailable(ResolutionReason.DISCOVERY_TIMEOUT)
        return _unavailable(ResolutionReason.WORKER_FAILURE)


def _replace_file(source: Path, target: Path) -> None:
    """Atomic same-directory replacement; a module seam so failure tests need no global patch."""

    os.replace(source, target)


def build_media_runtime_resolver() -> MediaRuntimeResolver:
    """Construct the process resolver. Called only by the composition root; performs no I/O."""

    # CRITICAL: construction and package import stay inert. Discovery starts only from an explicit
    # media intent or settings query; scanning PATH or spawning the worker here would put a
    # filesystem walk and a child interpreter on every ComfyUI start, including hosts that never
    # use media features.
    return MediaRuntimeResolver(host_roots=ComfyHostRootPort(), worker=SubprocessDiscoveryWorker())


media_runtime_resolver = component(MEDIA_RUNTIME_RESOLVER, MediaRuntimeResolver)


__all__ = [
    "CONFIG_SCHEMA",
    "LEGACY_NAMES",
    "MANAGED_PROFILE_COMPONENT",
    "RESOLUTION_SCHEMA",
    "ComfyHostRootPort",
    "DiscoveryWorkerError",
    "DiscoveryWorkerPort",
    "HostRootError",
    "HostRootPort",
    "LegacyKind",
    "LegacyNormalization",
    "MediaRuntimeConfig",
    "MediaRuntimeConfigError",
    "MediaRuntimePrivateLayout",
    "MediaRuntimeResolution",
    "MediaRuntimeResolver",
    "MediaRuntimeSelection",
    "ResolutionReason",
    "ResolutionState",
    "RuntimeInputs",
    "SourceKind",
    "SubprocessDiscoveryWorker",
    "build_media_runtime_resolver",
    "capture_runtime_inputs",
    "encode_config",
    "media_runtime_resolver",
    "normalize_legacy_environment",
    "parse_config",
    "private_layout",
]
