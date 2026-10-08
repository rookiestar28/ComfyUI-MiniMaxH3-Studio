"""M25-30: zero-input media runtime resolution and private configuration.

Fixtures that substitute executable digests are hermetic contract evidence for the resolver; they
are not native qualification. The one native row admits the exact pinned pair only when a caller
supplies it explicitly, following the repository's existing real-binary convention.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import pytest

import comfyui_h3_context.adapters.media_runtime_discovery_worker as worker_module
import comfyui_h3_context.adapters.media_runtime_resolution as resolution_module
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.executable_admission import (
    ExecutableAdmissionError as AdmissionError,
)
from comfyui_h3_context.adapters.executable_admission import pin_exact_executable
from comfyui_h3_context.adapters.media_runtime_discovery_worker import (
    DiscoveryMode,
    DiscoveryOutcome,
    DiscoveryProtocolError,
    DiscoveryRequest,
    DiscoveryResponse,
    admit_request,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    lexical_local_directory,
)
from comfyui_h3_context.adapters.media_runtime_resolution import (
    CONFIG_SCHEMA,
    ComfyHostRootPort,
    DiscoveryWorkerError,
    HostRootError,
    LegacyKind,
    MediaRuntimeConfig,
    MediaRuntimeConfigError,
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    MediaRuntimeSelection,
    ResolutionReason,
    ResolutionState,
    RuntimeInputs,
    SourceKind,
    SubprocessDiscoveryWorker,
    capture_runtime_inputs,
    normalize_legacy_environment,
    parse_config,
    private_layout,
)
from comfyui_h3_context.core.av_reconstruction import qualified_ffmpeg_capability

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows executable admission")

FFMPEG_BYTES = b"hermetic ffmpeg fixture"
FFPROBE_BYTES = b"hermetic ffprobe fixture"
FFMPEG_SHA = hashlib.sha256(FFMPEG_BYTES).hexdigest()
FFPROBE_SHA = hashlib.sha256(FFPROBE_BYTES).hexdigest()
LEGACY_FFMPEG = "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH"
LEGACY_FFPROBE = "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH"
LEGACY_SCRATCH = "H3_CONTEXT_AUTHORIZED_MEDIA_SCRATCH_ROOT"
LEGACY_MEDIA = "H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME"
LEGACY_RENDER = "H3_CONTEXT_AUTHORIZED_RENDER_RUNTIME"


# ---------------------------------------------------------------------------------------------
# Fixtures


def make_pair(
    directory: Path, *, ffmpeg: bytes = FFMPEG_BYTES, ffprobe: bytes = FFPROBE_BYTES
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ffmpeg.exe").write_bytes(ffmpeg)
    (directory / "ffprobe.exe").write_bytes(ffprobe)
    return directory


class FakeRoots:
    def __init__(self, root: Path, served: tuple[Path, ...] = ()) -> None:
        self.root = root
        self.served = served
        self.private_error: ResolutionReason | None = None
        self.served_error = False

    def private_root(self) -> Path:
        if self.private_error is not None:
            raise HostRootError(self.private_error)
        return self.root

    def served_roots(self) -> tuple[Path, ...]:
        if self.served_error:
            raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)
        return self.served


class InProcessWorker:
    """Runs the real admission code in this process and records every request."""

    def __init__(self) -> None:
        self.requests: list[DiscoveryRequest] = []
        self.cancelled = 0
        self.drive_type: Callable[[PureWindowsPath], int] = lambda _path: 3

    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse:
        assert timeout_seconds > 0
        self.requests.append(request)
        return admit_request(request, drive_type=self.drive_type)

    def cancel(self) -> None:
        self.cancelled += 1


class GatedWorker:
    def __init__(self, response: DiscoveryResponse) -> None:
        self.response = response
        self.calls = 0
        self.cancelled = 0
        self.started = threading.Event()
        self.gate = threading.Event()
        self.lock = threading.Lock()

    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse:
        with self.lock:
            self.calls += 1
        self.started.set()
        if not self.gate.wait(10):
            raise DiscoveryWorkerError("discovery_timeout")
        return self.response

    def cancel(self) -> None:
        self.cancelled += 1
        self.gate.set()


ADMITTED = DiscoveryResponse(
    DiscoveryOutcome.ADMITTED, index=0, ffmpeg_identity=(1, 2, 3, 4), ffprobe_identity=(1, 5, 6, 7)
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def inputs(
    *,
    path: str | None = None,
    prefix: Path | None = None,
    legacy: dict[str, str] | None = None,
    platform: str = "win32",
    machine: str = "AMD64",
    bits: int = 64,
) -> RuntimeInputs:
    return RuntimeInputs(
        platform=platform,
        machine=machine,
        pointer_bits=bits,
        python_prefix=str(prefix) if prefix is not None else "C:\\h3-absent-prefix",
        path=path,
        legacy=tuple(sorted((legacy or {}).items())),
    )


def resolver(
    tmp_path: Path,
    runtime_inputs: RuntimeInputs,
    *,
    worker: object | None = None,
    clock: Callable[[], float] | None = None,
    roots: FakeRoots | None = None,
    current: list[RuntimeInputs] | None = None,
) -> tuple[MediaRuntimeResolver, FakeRoots, object]:
    host = roots or FakeRoots(
        tmp_path / "user" / "__h3_context",
        served=(
            tmp_path / "comfy" / "input",
            tmp_path / "comfy" / "output",
            tmp_path / "comfy" / "temp",
        ),
    )
    chosen = worker or InProcessWorker()
    holder = current if current is not None else [runtime_inputs]
    service = MediaRuntimeResolver(
        host_roots=host,
        worker=chosen,  # type: ignore[arg-type]
        package_parent=tmp_path / "package",
        inputs_provider=lambda: holder[-1],
        clock=clock or Clock(),
        ffmpeg_sha256=FFMPEG_SHA,
        ffprobe_sha256=FFPROBE_SHA,
    )
    return service, host, chosen


# ---------------------------------------------------------------------------------------------
# AC30-01: zero-input located pair with a private default scratch


@windows_only
def test_no_environment_or_config_locates_the_pair_with_private_default_scratch(
    tmp_path: Path,
) -> None:
    tools = make_pair(tmp_path / "tools")
    service, host, _worker = resolver(tmp_path, inputs(path=str(tools)))

    result = service.resolve()

    assert result.state is ResolutionState.LOCATED
    assert result.reason is ResolutionReason.PAIR_ADMITTED
    assert result.source_kind is SourceKind.SYSTEM_PATH
    assert result.ffmpeg_path == tools / "ffmpeg.exe"
    assert result.ffprobe_path == tools / "ffprobe.exe"
    assert result.scratch_root == private_layout(host.root).scratch_root
    assert result.ffmpeg_identity is not None and result.ffprobe_identity is not None
    # Discovery is read-only: no private root, config, scratch or store is created.
    assert not host.root.exists()
    assert not (tmp_path / "package").exists()


@windows_only
def test_the_real_supervised_worker_admits_the_pair_and_leaves_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = make_pair(tmp_path / "tools")
    spawned: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original(*args, **kwargs)  # type: ignore[call-overload]
        spawned.append(process)
        return process  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    worker = SubprocessDiscoveryWorker()
    service = MediaRuntimeResolver(
        host_roots=FakeRoots(tmp_path / "user" / "__h3_context"),
        worker=worker,
        inputs_provider=lambda: inputs(path=str(tools)),
        ffmpeg_sha256=FFMPEG_SHA,
        ffprobe_sha256=FFPROBE_SHA,
    )

    result = service.resolve()

    assert result.state is ResolutionState.LOCATED
    assert result.source_kind is SourceKind.SYSTEM_PATH
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert worker._process is None and worker._stuck is None


def test_supplied_pinned_pair_is_admitted_with_the_production_profile(tmp_path: Path) -> None:
    ffmpeg = os.environ.get(LEGACY_FFMPEG)
    ffprobe = os.environ.get(LEGACY_FFPROBE)
    if os.name != "nt" or not ffmpeg or not ffprobe:
        pytest.skip("exact authorized media tool paths were not explicitly supplied")
    directory = Path(ffmpeg).parent
    assert Path(ffprobe).parent == directory
    service = MediaRuntimeResolver(
        host_roots=FakeRoots(tmp_path / "user" / "__h3_context"),
        worker=SubprocessDiscoveryWorker(),
        inputs_provider=lambda: inputs(path=str(directory)),
    )

    result = service.resolve()

    assert result.state is ResolutionState.LOCATED
    assert result.source_kind is SourceKind.SYSTEM_PATH
    assert result.ffmpeg_path == directory / "ffmpeg.exe"


# ---------------------------------------------------------------------------------------------
# AC30-02: precedence, authoritative errors and platform support


@windows_only
def test_precedence_managed_then_prefix_library_then_prefix_bin_then_path(tmp_path: Path) -> None:
    prefix = tmp_path / "python"
    path_tools = make_pair(tmp_path / "path-tools")
    service, host, _ = resolver(tmp_path, inputs(path=str(path_tools), prefix=prefix))
    layout = private_layout(host.root)
    make_pair(layout.managed_bin)
    make_pair(prefix / "Library" / "bin")
    make_pair(prefix / "bin")

    expected = (
        (layout.managed_bin, SourceKind.MANAGED),
        (prefix / "Library" / "bin", SourceKind.PYTHON_PREFIX),
        (prefix / "bin", SourceKind.PYTHON_PREFIX),
        (path_tools, SourceKind.SYSTEM_PATH),
    )
    for directory, source in expected:
        result = service.resolve(rescan=True)
        assert result.source_kind is source
        assert result.ffmpeg_path == directory / "ffmpeg.exe"
        shutil.rmtree(directory)
    assert service.resolve(rescan=True).reason is ResolutionReason.SUPPORTED_PAIR_MISSING


@windows_only
def test_a_broken_managed_install_falls_through_without_repair(tmp_path: Path) -> None:
    path_tools = make_pair(tmp_path / "path-tools")
    service, host, _ = resolver(tmp_path, inputs(path=str(path_tools)))
    managed = make_pair(private_layout(host.root).managed_bin, ffprobe=b"corrupted")
    before = (managed / "ffprobe.exe").read_bytes()

    result = service.resolve()

    assert result.source_kind is SourceKind.SYSTEM_PATH
    assert (managed / "ffprobe.exe").read_bytes() == before


@windows_only
def test_explicit_legacy_pair_is_authoritative_over_config_and_path(tmp_path: Path) -> None:
    explicit = make_pair(tmp_path / "explicit")
    path_tools = make_pair(tmp_path / "path-tools")
    legacy = {
        LEGACY_MEDIA: "1",
        LEGACY_FFMPEG: str(explicit / "ffmpeg.exe"),
        LEGACY_FFPROBE: str(explicit / "ffprobe.exe"),
    }
    service, _, _ = resolver(tmp_path, inputs(path=str(path_tools), legacy=legacy))

    result = service.resolve()

    assert result.state is ResolutionState.LOCATED
    assert result.source_kind is SourceKind.EXPLICIT_OVERRIDE
    assert result.ffmpeg_path == explicit / "ffmpeg.exe"


@windows_only
def test_an_invalid_explicit_pair_never_falls_back_to_a_valid_path_pair(tmp_path: Path) -> None:
    explicit = make_pair(tmp_path / "explicit", ffmpeg=b"not the pinned build")
    path_tools = make_pair(tmp_path / "path-tools")
    legacy = {
        LEGACY_FFMPEG: str(explicit / "ffmpeg.exe"),
        LEGACY_FFPROBE: str(explicit / "ffprobe.exe"),
    }
    service, _, worker = resolver(tmp_path, inputs(path=str(path_tools), legacy=legacy))

    result = service.resolve()

    assert result.state is ResolutionState.INVALID_CONFIG
    assert result.reason is ResolutionReason.OVERRIDE_UNSUPPORTED_PAIR
    assert result.source_kind is SourceKind.EXPLICIT_OVERRIDE
    assert isinstance(worker, InProcessWorker)
    assert [request.mode for request in worker.requests] == [DiscoveryMode.EXPLICIT]


@windows_only
def test_persisted_local_selection_is_authoritative_and_does_not_fall_back(tmp_path: Path) -> None:
    local = make_pair(tmp_path / "local")
    path_tools = make_pair(tmp_path / "path-tools")
    service, _, worker = resolver(tmp_path, inputs(path=str(path_tools)))
    service.write_config(
        expected_revision=0, selection=MediaRuntimeSelection.LOCAL, directory=str(local)
    )

    located = service.resolve()
    assert located.source_kind is SourceKind.LOCAL_SELECTION
    assert located.ffmpeg_path == local / "ffmpeg.exe"

    (local / "ffprobe.exe").write_bytes(b"replaced after selection")
    assert isinstance(worker, InProcessWorker)
    worker.requests.clear()
    refused = service.resolve(rescan=True)
    assert refused.state is ResolutionState.INVALID_CONFIG
    assert refused.reason is ResolutionReason.LOCAL_SELECTION_UNSUPPORTED_PAIR
    assert [request.mode for request in worker.requests] == [DiscoveryMode.EXPLICIT]

    shutil.rmtree(local)
    missing = service.resolve(rescan=True)
    assert missing.reason is ResolutionReason.LOCAL_SELECTION_INVALID_PATH


@windows_only
@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (b"{not json", ResolutionReason.CONFIG_CORRUPT),
        (
            json.dumps({"schema": "h3.context.media_runtime_config.v9"}).encode(),
            ResolutionReason.CONFIG_UNSUPPORTED,
        ),
    ),
)
def test_a_corrupt_or_unsupported_config_is_reported_and_preserved(
    tmp_path: Path, payload: bytes, reason: ResolutionReason
) -> None:
    path_tools = make_pair(tmp_path / "path-tools")
    service, host, worker = resolver(tmp_path, inputs(path=str(path_tools)))
    config = private_layout(host.root).config_file
    config.parent.mkdir(parents=True)
    config.write_bytes(payload)

    result = service.resolve()

    assert result.state is ResolutionState.INVALID_CONFIG
    assert result.reason is reason
    assert config.read_bytes() == payload
    assert isinstance(worker, InProcessWorker) and worker.requests == []
    with pytest.raises(MediaRuntimeConfigError):
        service.write_config(expected_revision=0, selection=MediaRuntimeSelection.AUTO)
    assert config.read_bytes() == payload


@pytest.mark.parametrize(
    ("platform", "machine", "bits"),
    (
        ("linux", "x86_64", 64),
        ("darwin", "arm64", 64),
        ("win32", "ARM64", 64),
        ("win32", "AMD64", 32),
    ),
)
def test_unsupported_platforms_never_probe(
    tmp_path: Path, platform: str, machine: str, bits: int
) -> None:
    service, _, worker = resolver(
        tmp_path, inputs(path="C:\\tools", platform=platform, machine=machine, bits=bits)
    )

    result = service.resolve()

    assert result.state is ResolutionState.UNAVAILABLE
    assert result.reason is ResolutionReason.UNSUPPORTED_PLATFORM
    assert isinstance(worker, InProcessWorker) and worker.requests == []
    with pytest.raises(MediaRuntimeConfigError, match="unsupported_platform"):
        service.read_config()


def _legacy(**values: str) -> dict[str, str]:
    names = {
        "media": LEGACY_MEDIA,
        "render": LEGACY_RENDER,
        "ffmpeg": LEGACY_FFMPEG,
        "ffprobe": LEGACY_FFPROBE,
        "scratch": LEGACY_SCRATCH,
    }
    return {names[key]: value for key, value in values.items()}


@pytest.mark.parametrize(
    ("values", "kind", "reason"),
    (
        ({}, LegacyKind.AUTO, None),
        (_legacy(media="1"), LegacyKind.AUTO, None),
        (_legacy(render="1"), LegacyKind.AUTO, None),
        (_legacy(media="1", render="1"), LegacyKind.AUTO, None),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.exe", ffprobe="C:\\t\\ffprobe.exe"),
            LegacyKind.EXPLICIT,
            None,
        ),
        (
            _legacy(media="1", ffmpeg="C:\\t\\ffmpeg.exe", ffprobe="C:\\T\\FFPROBE.EXE"),
            LegacyKind.EXPLICIT,
            None,
        ),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.exe", ffprobe="C:\\t\\ffprobe.exe", scratch="D:\\s"),
            LegacyKind.EXPLICIT,
            None,
        ),
        (_legacy(media="0"), LegacyKind.INVALID, ResolutionReason.OVERRIDE_INVALID_MARKER),
        (_legacy(render="true"), LegacyKind.INVALID, ResolutionReason.OVERRIDE_INVALID_MARKER),
        (_legacy(media=""), LegacyKind.INVALID, ResolutionReason.OVERRIDE_INVALID_MARKER),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.exe"),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_INCOMPLETE,
        ),
        (_legacy(scratch="D:\\s"), LegacyKind.INVALID, ResolutionReason.OVERRIDE_INCOMPLETE),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.exe", ffprobe=" "),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_INCOMPLETE,
        ),
        (
            _legacy(ffmpeg="C:\\a\\ffmpeg.exe", ffprobe="C:\\b\\ffprobe.exe"),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_SPLIT_DIRECTORY,
        ),
        (
            _legacy(ffmpeg="relative\\ffmpeg.exe", ffprobe="relative\\ffprobe.exe"),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_INVALID_PATH,
        ),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.bat", ffprobe="C:\\t\\ffprobe.cmd"),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_INVALID_PATH,
        ),
        (
            _legacy(ffmpeg="\\\\server\\t\\ffmpeg.exe", ffprobe="\\\\server\\t\\ffprobe.exe"),
            LegacyKind.INVALID,
            ResolutionReason.OVERRIDE_INVALID_PATH,
        ),
        (
            _legacy(ffmpeg="C:\\t\\ffmpeg.exe", ffprobe="C:\\t\\ffprobe.exe", scratch="scratch"),
            LegacyKind.INVALID,
            ResolutionReason.PRIVATE_ROOT_INVALID,
        ),
    ),
)
def test_legacy_normalization_table(
    values: dict[str, str], kind: LegacyKind, reason: ResolutionReason | None
) -> None:
    normalized = normalize_legacy_environment(values)

    assert normalized.kind is kind
    assert normalized.reason is reason
    assert "ffmpeg" not in repr(normalized)


@windows_only
def test_resolver_reports_legacy_errors_as_invalid_config_without_probing(tmp_path: Path) -> None:
    path_tools = make_pair(tmp_path / "path-tools")
    service, _, worker = resolver(tmp_path, inputs(path=str(path_tools), legacy=_legacy(media="0")))

    result = service.resolve()

    assert result.state is ResolutionState.INVALID_CONFIG
    assert result.reason is ResolutionReason.OVERRIDE_INVALID_MARKER
    assert isinstance(worker, InProcessWorker) and worker.requests == []


# ---------------------------------------------------------------------------------------------
# AC30-03: nothing unknown executes; admission keeps its guarantees


@windows_only
def test_incomplete_pairs_scripts_and_links_are_never_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi

    only_ffmpeg = tmp_path / "only-ffmpeg"
    only_ffmpeg.mkdir()
    (only_ffmpeg / "ffmpeg.exe").write_bytes(FFMPEG_BYTES)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "ffmpeg.bat").write_bytes(b"@echo off")
    (scripts / "ffprobe.cmd").write_bytes(b"@echo off")
    linked = tmp_path / "linked"
    linked.mkdir()
    real = make_pair(tmp_path / "real")
    os.link(real / "ffmpeg.exe", linked / "ffmpeg.exe")
    (linked / "ffprobe.exe").write_bytes(FFPROBE_BYTES)
    junction = tmp_path / "junction"
    # Windows-only fixture APIs are absent from POSIX stubs, but must stay real junctions.
    getattr(_winapi, "CreateJunction")(  # noqa: B009
        str(make_pair(tmp_path / "behind-junction")), str(junction)
    )
    hashed: list[Path] = []

    def spy(path: Path, expected: str, **kwargs: object) -> object:
        hashed.append(path)
        return pin_exact_executable(path, expected, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "pin_exact_executable", spy)
    request = DiscoveryRequest(
        mode=DiscoveryMode.AUTO,
        candidates=tuple(str(item) for item in (only_ffmpeg, scripts, linked, junction)),
        ffmpeg_sha256=FFMPEG_SHA,
        ffprobe_sha256=FFPROBE_SHA,
        budget_ms=30_000,
    )

    response = admit_request(request)

    assert response.outcome is DiscoveryOutcome.EXHAUSTED
    assert hashed == []


@windows_only
def test_oversized_executables_are_refused_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = make_pair(tmp_path / "tools")
    monkeypatch.setattr(worker_module, "MAX_EXECUTABLE_BYTES", len(FFMPEG_BYTES) - 1)
    monkeypatch.setattr(
        worker_module,
        "pin_exact_executable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not hash")),
    )

    response = admit_request(
        DiscoveryRequest(DiscoveryMode.EXPLICIT, (str(tools),), FFMPEG_SHA, FFPROBE_SHA, 30_000)
    )

    assert response.outcome is DiscoveryOutcome.UNSUPPORTED_PAIR


def test_network_drives_are_refused_without_touching_the_filesystem() -> None:
    touched: list[PureWindowsPath] = []

    def remote(path: PureWindowsPath) -> int:
        touched.append(path)
        return 4

    response = admit_request(
        DiscoveryRequest(DiscoveryMode.EXPLICIT, ("Z:\\tools",), FFMPEG_SHA, FFPROBE_SHA, 30_000),
        drive_type=remote,
    )

    assert response.outcome is DiscoveryOutcome.UNSAFE_PATH
    assert touched == [PureWindowsPath("Z:\\tools")]


@pytest.mark.parametrize(
    "value",
    (
        "",
        "relative\\bin",
        "C:relative",
        "\\\\server\\share\\bin",
        "\\\\?\\C:\\bin",
        "\\\\.\\C:\\bin",
        "C:\\a\\..\\bin",
        "C:\\a\\.\\bin",
        "C:\\bin\\file.exe:stream",
        "C:\\trailing \\bin",
        "C:\\trailing.\\bin",
        "C:\\CON\\bin",
        "C:\\%SystemRoot%\\bin",
        '"C:\\quoted\\bin"',
        "C:\\" + "a" * 5000,
    ),
)
def test_lexical_locator_refuses_unsafe_forms(value: str) -> None:
    assert lexical_local_directory(value) is None


def test_lexical_locator_normalizes_a_safe_directory() -> None:
    assert lexical_local_directory("c:/Tools/ffmpeg/bin/") == PureWindowsPath(
        "C:\\Tools\\ffmpeg\\bin"
    )


@windows_only
def test_the_adapter_pin_keeps_its_closed_errors_after_extraction(tmp_path: Path) -> None:
    import comfyui_h3_context.adapters.av_reconstruction_media as media_module

    executable = make_pair(tmp_path / "tools") / "ffmpeg.exe"

    # An adapter error raised while pinned propagates unchanged ...
    with pytest.raises(media_module.AVMediaAdapterError, match="inspection_invalid"):
        with media_module._pin_exact_executable(executable, FFMPEG_SHA):
            raise media_module.AVMediaAdapterError("inspection_invalid")
    # ... an OS failure while pinned is still a capability change ...
    with pytest.raises(media_module.AVMediaAdapterError, match="adapter_capability_changed"):
        with media_module._pin_exact_executable(executable, FFMPEG_SHA):
            raise OSError("spawn failed")
    # ... any other exception is not re-coded ...
    with pytest.raises(KeyError):
        with media_module._pin_exact_executable(executable, FFMPEG_SHA):
            raise KeyError("caller bug")
    # ... and a digest mismatch keeps the adapter's type and code.
    with pytest.raises(media_module.AVMediaAdapterError, match="adapter_capability_changed"):
        with media_module._pin_exact_executable(executable, FFPROBE_SHA):
            pass


@windows_only
def test_a_located_result_does_not_authorize_later_use_of_changed_bytes(tmp_path: Path) -> None:
    tools = make_pair(tmp_path / "tools")
    service, _, _ = resolver(tmp_path, inputs(path=str(tools)))
    located = service.resolve()
    assert located.ffprobe_path is not None

    located.ffprobe_path.write_bytes(b"replaced after discovery")

    # The cached result is a locator, not a capability: the next exact pin refuses the bytes,
    # and a fresh resolution no longer admits the directory.
    with pytest.raises(AdmissionError, match="adapter_capability_changed"):
        with pin_exact_executable(located.ffprobe_path, FFPROBE_SHA):
            pass
    assert service.resolve(rescan=True).reason is ResolutionReason.SUPPORTED_PAIR_MISSING


# ---------------------------------------------------------------------------------------------
# AC30-04: finite work


@windows_only
def test_path_snapshot_over_the_byte_bound_is_a_limit_before_enumeration(tmp_path: Path) -> None:
    service, _, worker = resolver(tmp_path, inputs(path="C:\\x;" * 7000))

    result = service.resolve()

    assert result.reason is ResolutionReason.DISCOVERY_LIMIT
    assert isinstance(worker, InProcessWorker) and worker.requests == []
    # Nothing was searched, so not even the managed directory is known to be empty.
    assert result.managed_searched is False


@windows_only
def test_candidate_directories_are_deduplicated_and_capped(tmp_path: Path) -> None:
    entries = [f"C:\\h3-missing-{index}" for index in range(40)]
    duplicated = ";".join(entries[:2] + [entries[0].upper(), "", ".", "relative"] + entries[2:])
    service, _, worker = resolver(tmp_path, inputs(path=duplicated))

    result = service.resolve()

    assert isinstance(worker, InProcessWorker)
    (request,) = worker.requests
    assert len(request.candidates) == 32
    assert len({candidate.casefold() for candidate in request.candidates}) == 32
    # Truncated search: never an exhaustive absence claim ...
    assert result.reason is ResolutionReason.DISCOVERY_LIMIT
    # ... but the managed directory, always the first candidate, was searched and holds no pair.
    assert result.managed_searched is True


def test_only_a_limited_search_can_record_the_managed_directory_as_searched() -> None:
    for state, reason in (
        (ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING),
        (ResolutionState.UNAVAILABLE, ResolutionReason.DISCOVERY_TIMEOUT),
        (ResolutionState.DISCOVERING, ResolutionReason.DISCOVERY_IN_PROGRESS),
    ):
        with pytest.raises(ValueError, match="managed"):
            MediaRuntimeResolution(state, reason, managed_searched=True)
    limited = MediaRuntimeResolution(
        ResolutionState.UNAVAILABLE, ResolutionReason.DISCOVERY_LIMIT, managed_searched=True
    )
    assert "managed" not in json.dumps(limited.to_wire())


@windows_only
def test_at_most_eight_complete_pairs_are_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directories = [make_pair(tmp_path / f"pair-{index}", ffmpeg=b"wrong") for index in range(9)]
    hashed: list[Path] = []

    def spy(path: Path, expected: str, **kwargs: object) -> object:
        hashed.append(path)
        return pin_exact_executable(path, expected, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "pin_exact_executable", spy)

    response = admit_request(
        DiscoveryRequest(
            DiscoveryMode.AUTO,
            tuple(str(item) for item in directories),
            FFMPEG_SHA,
            FFPROBE_SHA,
            30_000,
        )
    )

    assert response.outcome is DiscoveryOutcome.LIMIT
    assert hashed == [item / "ffmpeg.exe" for item in directories[:8]]


@windows_only
def test_a_pair_larger_than_the_remaining_byte_budget_is_not_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = make_pair(tmp_path / "tools")
    monkeypatch.setattr(
        worker_module,
        "pin_exact_executable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not hash")),
    )

    response = admit_request(
        DiscoveryRequest(
            DiscoveryMode.AUTO,
            (str(tools),),
            FFMPEG_SHA,
            FFPROBE_SHA,
            30_000,
            max_hash_bytes=len(FFMPEG_BYTES) + len(FFPROBE_BYTES) - 1,
        )
    )

    assert response.outcome is DiscoveryOutcome.LIMIT


def test_the_byte_budget_is_enforced_while_reading_beyond_the_declared_size() -> None:
    # A file that grows between stat and read must not overrun the resolution's I/O ceiling.
    request = DiscoveryRequest(
        DiscoveryMode.AUTO, ("C:\\tools",), FFMPEG_SHA, FFPROBE_SHA, 30_000, max_hash_bytes=100
    )
    budget = worker_module._Budget(request, lambda: 0.0)
    budget.reserve_pair(60)
    budget.charge(60)

    with pytest.raises(AdmissionError, match="discovery_limit"):
        budget.charge(41)


@windows_only
def test_the_worker_deadline_ends_the_walk_as_a_timeout(tmp_path: Path) -> None:
    tools = [make_pair(tmp_path / f"tools-{index}", ffmpeg=b"wrong") for index in range(3)]
    ticks = iter((0.0, 0.1, 0.2, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0))

    response = admit_request(
        DiscoveryRequest(
            DiscoveryMode.AUTO, tuple(str(item) for item in tools), FFMPEG_SHA, FFPROBE_SHA, 1_000
        ),
        clock=lambda: next(ticks),
    )

    assert response.outcome is DiscoveryOutcome.TIMEOUT


def _fake_package(tmp_path: Path, worker_source: str) -> Path:
    parent = tmp_path / "fake-package"
    adapters = parent / "comfyui_h3_context" / "adapters"
    adapters.mkdir(parents=True)
    (parent / "comfyui_h3_context" / "__init__.py").write_text("raise SystemExit(91)\n")
    (parent / "comfyui_h3_context" / "core").mkdir()
    (adapters / "media_runtime_discovery_worker.py").write_text(worker_source)
    return parent


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(DiscoveryMode.AUTO, ("C:\\h3-absent",), FFMPEG_SHA, FFPROBE_SHA, 1_000)


@windows_only
def test_a_hung_worker_is_killed_at_the_deadline_and_reaped(tmp_path: Path) -> None:
    parent = _fake_package(tmp_path, "import time\ndef main():\n    time.sleep(60)\n    return 0\n")
    worker = SubprocessDiscoveryWorker(package_parent=parent)

    with pytest.raises(DiscoveryWorkerError, match="discovery_timeout"):
        worker.run(_request(), timeout_seconds=0.5)

    assert worker._process is None and worker._stuck is None


@windows_only
@pytest.mark.parametrize(
    "source",
    (
        "import sys\ndef main():\n    sys.stdout.write('not json')\n    return 0\n",
        "import sys\ndef main():\n    sys.stdout.write('x' * 70000)\n    return 0\n",
        "def main():\n    return 3\n",
    ),
)
def test_malformed_worker_output_is_a_worker_failure(tmp_path: Path, source: str) -> None:
    worker = SubprocessDiscoveryWorker(package_parent=_fake_package(tmp_path, source))

    with pytest.raises(DiscoveryWorkerError, match="worker_failure"):
        worker.run(_request(), timeout_seconds=10)


def test_an_unreaped_worker_blocks_discovery_until_it_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stuck:
        alive = True

        def poll(self) -> int | None:
            return None if self.alive else 0

    stuck = Stuck()
    worker = SubprocessDiscoveryWorker()
    worker._stuck = stuck  # type: ignore[assignment]
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not spawn past a stuck worker")
        ),
    )

    with pytest.raises(DiscoveryWorkerError, match="worker_failure"):
        worker.run(_request(), timeout_seconds=1)

    stuck.alive = False
    with pytest.raises(AssertionError, match="must not spawn"):
        worker.run(_request(), timeout_seconds=1)
    assert worker._stuck is None


@windows_only
def test_the_worker_never_executes_the_package_initializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CRITICAL proof for the bootstrap guard: a copy of the real package whose __init__ exits 91.
    source = Path(resolution_module.__file__).resolve().parents[1]
    parent = tmp_path / "copy"
    shutil.copytree(
        source, parent / "comfyui_h3_context", ignore=shutil.ignore_patterns("__pycache__")
    )
    (parent / "comfyui_h3_context" / "__init__.py").write_text("raise SystemExit(91)\n")
    tools = make_pair(tmp_path / "tools")
    monkeypatch.setenv(LEGACY_MEDIA, "1")
    worker = SubprocessDiscoveryWorker(package_parent=parent)

    response = worker.run(
        DiscoveryRequest(DiscoveryMode.AUTO, (str(tools),), FFMPEG_SHA, FFPROBE_SHA, 10_000),
        timeout_seconds=20,
    )

    assert response.outcome is DiscoveryOutcome.ADMITTED


# ---------------------------------------------------------------------------------------------
# AC30-05: coalescing, invalidation, expiry and cancellation


def _gated_resolver(
    tmp_path: Path, response: DiscoveryResponse = ADMITTED
) -> tuple[MediaRuntimeResolver, GatedWorker, Clock, list[RuntimeInputs]]:
    worker = GatedWorker(response)
    clock = Clock()
    current = [inputs(path="C:\\h3-tools")]
    service, _, _ = resolver(tmp_path, current[0], worker=worker, clock=clock, current=current)
    return service, worker, clock, current


def _in_thread(call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(call()))
    thread.start()
    return thread, results


@windows_only
def test_concurrent_identical_queries_share_one_worker_run(tmp_path: Path) -> None:
    service, worker, _, _ = _gated_resolver(tmp_path)
    first, first_result = _in_thread(service.resolve)
    assert worker.started.wait(5)
    second, second_result = _in_thread(service.resolve)
    assert service.peek() is not None and service.peek().state is ResolutionState.DISCOVERING  # type: ignore[union-attr]

    worker.gate.set()
    first.join(5)
    second.join(5)

    assert worker.calls == 1
    assert first_result[0] is second_result[0]
    assert first_result[0].state is ResolutionState.LOCATED  # type: ignore[attr-defined]


@windows_only
def test_changed_inputs_supersede_the_inflight_request_without_queueing(tmp_path: Path) -> None:
    service, worker, _, current = _gated_resolver(tmp_path)
    first, first_result = _in_thread(service.resolve)
    assert worker.started.wait(5)

    current.append(inputs(path="C:\\h3-other-tools"))
    second, second_result = _in_thread(service.resolve)
    first.join(5)
    second.join(5)

    assert worker.cancelled >= 1
    assert first_result[0].state is ResolutionState.DISCOVERING  # type: ignore[attr-defined]
    assert second_result[0].state is ResolutionState.LOCATED  # type: ignore[attr-defined]
    assert worker.calls == 2
    assert service.peek() is second_result[0]


@windows_only
def test_invalidation_during_discovery_prevents_stale_publication(tmp_path: Path) -> None:
    service, worker, _, _ = _gated_resolver(tmp_path)
    thread, result = _in_thread(service.resolve)
    assert worker.started.wait(5)

    service.invalidate()
    thread.join(5)

    assert result[0].state is ResolutionState.DISCOVERING  # type: ignore[attr-defined]
    assert service.peek() is None


@windows_only
@pytest.mark.parametrize(
    ("response", "lifetime"),
    (
        (ADMITTED, 60.0),
        (DiscoveryResponse(DiscoveryOutcome.EXHAUSTED), 5.0),
    ),
)
def test_results_expire_after_their_positive_or_negative_lifetime(
    tmp_path: Path, response: DiscoveryResponse, lifetime: float
) -> None:
    service, worker, clock, _ = _gated_resolver(tmp_path, response)
    worker.gate.set()

    first = service.resolve()
    clock.now += lifetime - 0.5
    assert service.resolve() is first
    clock.now += 1.0
    assert service.peek() is None
    service.resolve()

    assert worker.calls == 2


@windows_only
def test_rescan_and_config_writes_invalidate_the_cache(tmp_path: Path) -> None:
    service, worker, _, _ = _gated_resolver(tmp_path)
    worker.gate.set()

    service.resolve()
    service.resolve(rescan=True)
    assert worker.calls == 2
    service.write_config(expected_revision=0, selection=MediaRuntimeSelection.AUTO)
    assert service.peek() is None
    service.resolve()
    assert worker.calls == 3


# ---------------------------------------------------------------------------------------------
# AC30-06: private configuration


def _config_bytes(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "schema": CONFIG_SCHEMA,
        "revision": 1,
        "selection": "auto",
        "directory": None,
    }
    value.update(overrides)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    ("payload", "code"),
    (
        (b"", "config_corrupt"),
        (b"[]", "config_corrupt"),
        (b"x" * (16 * 1024 + 1), "config_corrupt"),
        (_config_bytes()[:-1] + b', "revision": 2}', "config_corrupt"),
        (_config_bytes(extra=1), "config_corrupt"),
        (_config_bytes(revision=True), "config_corrupt"),
        (_config_bytes(revision=0), "config_corrupt"),
        (_config_bytes(revision=1.0), "config_corrupt"),
        (
            b'{"schema": "h3.context.media_runtime_config.v1", "revision": NaN, '
            b'"selection": "auto", "directory": null}',
            "config_corrupt",
        ),
        (b"[" * 5000 + b"]" * 5000, "config_corrupt"),
        (_config_bytes(selection="auto", directory="C:\\tools"), "config_corrupt"),
        (_config_bytes(selection="local", directory=None), "config_corrupt"),
        (_config_bytes(selection="local", directory="relative"), "config_corrupt"),
        (_config_bytes(selection="managed"), "config_corrupt"),
        (_config_bytes(schema="h3.context.media_runtime_config.v2"), "config_unsupported"),
        (b"\xff\xfe", "config_corrupt"),
    ),
)
def test_config_parsing_is_strict(payload: bytes, code: str) -> None:
    with pytest.raises(MediaRuntimeConfigError) as raised:
        parse_config(payload)
    assert raised.value.code == code


def test_config_v1_round_trips_without_carrying_anything_else() -> None:
    config = parse_config(_config_bytes(revision=3, selection="local", directory="D:\\Tools\\bin"))

    assert config == MediaRuntimeConfig(3, MediaRuntimeSelection.LOCAL, "D:\\Tools\\bin")
    assert "Tools" not in repr(config)
    encoded = json.loads(resolution_module.encode_config(config))
    assert set(encoded) == {"schema", "revision", "selection", "directory"}


@windows_only
def test_config_write_round_trip_revision_and_conflict(tmp_path: Path) -> None:
    local = make_pair(tmp_path / "local")
    service, host, _ = resolver(tmp_path, inputs())
    config_file = private_layout(host.root).config_file

    assert service.read_config() is None
    service.resolve()
    assert not config_file.exists()

    first = service.write_config(
        expected_revision=0, selection=MediaRuntimeSelection.LOCAL, directory=str(local)
    )
    assert first.revision == 1 and service.read_config() == first
    with pytest.raises(MediaRuntimeConfigError, match="config_conflict"):
        service.write_config(expected_revision=0, selection=MediaRuntimeSelection.AUTO)
    second = service.write_config(expected_revision=1, selection=MediaRuntimeSelection.AUTO)
    assert second == MediaRuntimeConfig(2, MediaRuntimeSelection.AUTO, None)
    assert [path.name for path in config_file.parent.iterdir()] == ["config.json"]


@windows_only
def test_a_local_selection_is_validated_before_commit(tmp_path: Path) -> None:
    unsupported = make_pair(tmp_path / "unsupported", ffprobe=b"other build")
    service, host, _ = resolver(tmp_path, inputs())

    with pytest.raises(MediaRuntimeConfigError, match="selection_unsupported_pair"):
        service.write_config(
            expected_revision=0, selection=MediaRuntimeSelection.LOCAL, directory=str(unsupported)
        )
    with pytest.raises(MediaRuntimeConfigError, match="selection_invalid_path"):
        service.write_config(
            expected_revision=0, selection=MediaRuntimeSelection.LOCAL, directory="relative"
        )
    assert not private_layout(host.root).config_file.exists()


@windows_only
def test_a_failed_replacement_preserves_the_previous_bytes_and_removes_its_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, host, _ = resolver(tmp_path, inputs())
    service.write_config(expected_revision=0, selection=MediaRuntimeSelection.AUTO)
    config_file = private_layout(host.root).config_file
    before = config_file.read_bytes()

    def fail(_source: Path, _target: Path) -> None:
        raise PermissionError("replacement denied")

    monkeypatch.setattr(resolution_module, "_replace_file", fail)
    with pytest.raises(MediaRuntimeConfigError, match="config_write_failed"):
        service.write_config(expected_revision=1, selection=MediaRuntimeSelection.AUTO)

    assert config_file.read_bytes() == before
    assert [path.name for path in config_file.parent.iterdir()] == ["config.json"]


@windows_only
def test_a_concurrent_external_change_at_commit_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, host, _ = resolver(tmp_path, inputs())
    service.write_config(expected_revision=0, selection=MediaRuntimeSelection.AUTO)
    config_file = private_layout(host.root).config_file
    original_snapshot = service._config_snapshot
    calls = {"count": 0}

    def racing_snapshot(layout: object) -> object:
        calls["count"] += 1
        if calls["count"] == 2:
            config_file.write_bytes(_config_bytes(revision=7))
        return original_snapshot(layout)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_config_snapshot", racing_snapshot)
    with pytest.raises(MediaRuntimeConfigError, match="config_conflict"):
        service.write_config(expected_revision=1, selection=MediaRuntimeSelection.AUTO)

    assert parse_config(config_file.read_bytes()).revision == 7


@windows_only
def test_no_private_locator_appears_in_public_results_or_reprs(tmp_path: Path) -> None:
    tools = make_pair(tmp_path / "private-tool-directory")
    service, host, _ = resolver(tmp_path, inputs(path=str(tools)))

    result = service.resolve()
    rendered = " ".join(
        (
            repr(result),
            json.dumps(result.to_wire()),
            repr(service),
            repr(private_layout(host.root)),
            repr(inputs(path=str(tools))),
        )
    )

    assert "private-tool-directory" not in rendered
    assert str(tmp_path) not in rendered
    assert set(result.to_wire()) == {"schema", "state", "reason", "source_kind"}


# ---------------------------------------------------------------------------------------------
# AC30-07: host private root and legacy scratch


def test_host_without_folder_paths_or_the_private_api_is_unsupported() -> None:
    # The port reads a host object through an injected lookup; no ComfyUI module is imitated.
    with pytest.raises(HostRootError) as missing_api:
        ComfyHostRootPort(module_provider=lambda: types.SimpleNamespace()).private_root()
    assert missing_api.value.reason is ResolutionReason.UNSUPPORTED_HOST

    with pytest.raises(HostRootError) as missing_module:
        ComfyHostRootPort(module_provider=lambda: None).private_root()
    assert missing_module.value.reason is ResolutionReason.UNSUPPORTED_HOST


@pytest.mark.parametrize("value", ("relative\\user\\__h3_context", "\\\\server\\user", None, 7))
def test_an_unsafe_private_root_is_refused(value: object) -> None:
    host = types.SimpleNamespace(get_system_user_directory=lambda _name: value)

    with pytest.raises(HostRootError) as raised:
        ComfyHostRootPort(module_provider=lambda: host).private_root()

    assert raised.value.reason is ResolutionReason.PRIVATE_ROOT_INVALID


def test_the_host_private_root_is_requested_by_the_fixed_name() -> None:
    requested: list[str] = []

    def system_user_directory(name: str) -> str:
        requested.append(name)
        return "C:\\ComfyUI\\user\\__h3_context"

    host = types.SimpleNamespace(get_system_user_directory=system_user_directory)

    # IMPORTANT: these drive-path fixtures exercise Windows grammar, even on a POSIX test runner.
    assert ComfyHostRootPort(module_provider=lambda: host, os_name="nt").private_root() == Path(
        "C:\\ComfyUI\\user\\__h3_context"
    )
    assert requested == ["h3_context"]


def test_served_roots_require_every_host_directory_getter() -> None:
    complete = types.SimpleNamespace(
        get_input_directory=lambda: "C:\\ComfyUI\\input",
        get_output_directory=lambda: "C:\\ComfyUI\\output",
        get_temp_directory=lambda: "C:\\ComfyUI\\temp\\..\\temp",
    )
    assert ComfyHostRootPort(module_provider=lambda: complete, os_name="nt").served_roots() == (
        Path("C:\\ComfyUI\\input"),
        Path("C:\\ComfyUI\\output"),
        Path("C:\\ComfyUI\\temp"),
    )
    partial = types.SimpleNamespace(get_input_directory=lambda: "C:\\ComfyUI\\input")
    with pytest.raises(HostRootError) as raised:
        ComfyHostRootPort(module_provider=lambda: partial, os_name="nt").served_roots()
    assert raised.value.reason is ResolutionReason.PRIVATE_ROOT_INVALID


@windows_only
@pytest.mark.parametrize(
    "reason", (ResolutionReason.UNSUPPORTED_HOST, ResolutionReason.PRIVATE_ROOT_INVALID)
)
def test_a_missing_private_root_is_unavailable_with_no_public_fallback(
    tmp_path: Path, reason: ResolutionReason
) -> None:
    tools = make_pair(tmp_path / "tools")
    service, host, worker = resolver(tmp_path, inputs(path=str(tools)))
    host.private_error = reason

    result = service.resolve()

    assert result.state is ResolutionState.UNAVAILABLE
    assert result.reason is reason
    assert isinstance(worker, InProcessWorker) and worker.requests == []
    assert not (tmp_path / "comfy").exists()


@windows_only
def test_a_linked_private_root_component_is_refused(tmp_path: Path) -> None:
    import _winapi

    tools = make_pair(tmp_path / "tools")
    real = tmp_path / "real-user"
    real.mkdir()
    link = tmp_path / "linked-user"
    getattr(_winapi, "CreateJunction")(str(real), str(link))  # noqa: B009
    service, _, worker = resolver(
        tmp_path, inputs(path=str(tools)), roots=FakeRoots(link / "__h3_context")
    )

    result = service.resolve()

    assert result.reason is ResolutionReason.PRIVATE_ROOT_INVALID
    assert isinstance(worker, InProcessWorker) and worker.requests == []


@windows_only
@pytest.mark.parametrize("inside", ("input", "output", "temp", "package"))
def test_a_legacy_scratch_overlapping_a_served_root_or_the_package_is_refused(
    tmp_path: Path, inside: str
) -> None:
    tools = make_pair(tmp_path / "tools")
    base = tmp_path / "package" if inside == "package" else tmp_path / "comfy" / inside
    legacy = {
        LEGACY_FFMPEG: str(tools / "ffmpeg.exe"),
        LEGACY_FFPROBE: str(tools / "ffprobe.exe"),
        LEGACY_SCRATCH: str(base / "h3-scratch"),
    }
    service, _, worker = resolver(tmp_path, inputs(legacy=legacy))

    result = service.resolve()

    assert result.state is ResolutionState.INVALID_CONFIG
    assert result.reason is ResolutionReason.PRIVATE_ROOT_INVALID
    assert isinstance(worker, InProcessWorker) and worker.requests == []
    assert not base.exists()


@windows_only
def test_a_legacy_scratch_is_refused_when_served_roots_cannot_be_established(
    tmp_path: Path,
) -> None:
    tools = make_pair(tmp_path / "tools")
    legacy = {
        LEGACY_FFMPEG: str(tools / "ffmpeg.exe"),
        LEGACY_FFPROBE: str(tools / "ffprobe.exe"),
        LEGACY_SCRATCH: str(tmp_path / "private-scratch"),
    }
    service, host, _ = resolver(tmp_path, inputs(legacy=legacy))
    host.served_error = True

    assert service.resolve().reason is ResolutionReason.PRIVATE_ROOT_INVALID


@windows_only
def test_a_valid_legacy_binding_keeps_its_scratch_and_creates_nothing(tmp_path: Path) -> None:
    import _winapi

    tools = make_pair(tmp_path / "tools")
    scratch = tmp_path / "private-scratch" / "h3"
    legacy = {
        LEGACY_MEDIA: "1",
        LEGACY_FFMPEG: str(tools / "ffmpeg.exe"),
        LEGACY_FFPROBE: str(tools / "ffprobe.exe"),
        LEGACY_SCRATCH: str(scratch),
    }
    service, _, _ = resolver(tmp_path, inputs(legacy=legacy))

    result = service.resolve()
    assert result.state is ResolutionState.LOCATED
    assert result.scratch_root == scratch
    assert not scratch.parent.exists()

    target = tmp_path / "elsewhere"
    target.mkdir()
    getattr(_winapi, "CreateJunction")(str(target), str(tmp_path / "private-scratch"))  # noqa: B009
    refused = service.resolve(rescan=True)
    assert refused.state is ResolutionState.INVALID_CONFIG
    assert refused.reason is ResolutionReason.PRIVATE_ROOT_INVALID


# ---------------------------------------------------------------------------------------------
# AC30-08: inert wiring


def test_constructing_the_process_resolver_spawns_nothing_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no worker at construction")),
    )
    monkeypatch.setattr(
        resolution_module,
        "capture_runtime_inputs",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no input capture")),
    )
    saved = composition_root.installed(composition_root.MEDIA_RUNTIME_RESOLVER)
    try:
        composition_root.reset(composition_root.MEDIA_RUNTIME_RESOLVER)
        built = composition_root.get(composition_root.MEDIA_RUNTIME_RESOLVER)
        assert isinstance(built, MediaRuntimeResolver)
        assert resolution_module.media_runtime_resolver() is built
        assert built.peek() is None
    finally:
        composition_root.reset(composition_root.MEDIA_RUNTIME_RESOLVER)
        if saved is not None:
            composition_root.install(composition_root.MEDIA_RUNTIME_RESOLVER, saved)


def test_package_import_neither_builds_the_resolver_nor_starts_a_worker() -> None:
    script = (
        "import subprocess\n"
        # asyncio subclasses Popen at import, so the stand-in must stay a class.
        "class Refuse(subprocess.Popen):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise SystemExit('worker spawned during import')\n"
        "subprocess.Popen = Refuse\n"
        "import comfyui_h3_context\n"
        "from comfyui_h3_context.adapters import composition_root as root\n"
        "import comfyui_h3_context.adapters.media_runtime_resolution\n"
        "assert root.installed(root.MEDIA_RUNTIME_RESOLVER) is None\n"
        "print('inert')\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("H3_CONTEXT_")
    }
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(resolution_module.__file__).resolve().parents[2],
        env=environment,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().endswith("inert")


def test_the_managed_profile_component_names_the_qualified_build() -> None:
    capability = qualified_ffmpeg_capability()

    assert resolution_module.MANAGED_PROFILE_COMPONENT == f"gyan-full-{capability.build_version}"
    assert capability.distribution == "gyan.dev-full_build"


def test_capture_runtime_inputs_reads_only_path_and_the_legacy_names() -> None:
    captured = capture_runtime_inputs(
        {"Path": "C:\\a", LEGACY_MEDIA: "1", "H3_CONTEXT_UNRELATED": "x", "SECRET_TOKEN": "y"}
    )

    assert captured.path == "C:\\a"
    assert captured.legacy == ((LEGACY_MEDIA, "1"),)


# ---------------------------------------------------------------------------------------------
# Protocol


def test_protocol_round_trips_and_refuses_extra_or_malformed_fields() -> None:
    request = DiscoveryRequest(
        DiscoveryMode.EXPLICIT, ("C:\\tools",), FFMPEG_SHA, FFPROBE_SHA, 1_000, scratch="D:\\s"
    )
    assert decode_request(encode_request(request)) == request
    assert decode_response(encode_response(ADMITTED)) == ADMITTED

    mutated = json.loads(encode_request(request))
    mutated["extra"] = True
    with pytest.raises(DiscoveryProtocolError):
        decode_request(json.dumps(mutated).encode())
    for broken in (
        {**json.loads(encode_request(request)), "candidates": ["relative"]},
        {**json.loads(encode_request(request)), "ffmpeg_sha256": "x" * 64},
        {**json.loads(encode_request(request)), "budget_ms": 0},
        {
            **json.loads(encode_request(request)),
            "mode": "explicit",
            "candidates": ["C:\\a", "C:\\b"],
        },
    ):
        with pytest.raises(DiscoveryProtocolError):
            decode_request(json.dumps(broken).encode())
    with pytest.raises(DiscoveryProtocolError):
        DiscoveryResponse(DiscoveryOutcome.EXHAUSTED, index=0)
    with pytest.raises(DiscoveryProtocolError):
        decode_response(b'{"schema": "x"}')
