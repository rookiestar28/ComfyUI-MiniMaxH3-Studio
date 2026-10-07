"""POSIX host-root admission and the App Mode closure composed with the real host-root port.

The closure tests build the coordinator with its production `_host_private_root` and
`_host_artifact_store` factories over a `folder_paths` double, so they run the same composition a
live host runs. On Linux they are the regression for defect D1: before M23-67 every POSIX root went
through the Windows drive-path grammar and the closure failed with `host_storage_unavailable`.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_m23_15_sequence_coordinator import (
    _action,
    _prepare,
    _production_workspace,
    _required_response,
    _submit,
)

import comfyui_h3_context.adapters.comfyui_sequence_coordinator as coordinator_adapter
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.comfyui_sequence_coordinator import (
    ObservedVideoArtifact,
    SequenceCoordinatorError,
    SequenceCoordinatorRegistry,
)
from comfyui_h3_context.adapters.media_runtime_resolution import (
    PRIVATE_ROOT_NAME,
    ComfyHostRootPort,
    HostRootError,
    ResolutionReason,
    _host_locator,
    _overlaps,
)
from comfyui_h3_context.adapters.posix_host_paths import (
    MAX_POSIX_LOCATOR_BYTES,
    MAX_POSIX_SEGMENT_BYTES,
    lexical_posix_directory,
)


def _exact_bytes_path(total: int) -> str:
    """An absolute path of exactly `total` UTF-8 bytes built from maximal 255-byte segments."""

    full, rest = divmod(total, MAX_POSIX_SEGMENT_BYTES + 1)
    segments = ["a" * MAX_POSIX_SEGMENT_BYTES] * full
    if rest:
        segments.append("a" * (rest - 1))
    value = "/" + "/".join(segments)
    assert len(value.encode("utf-8")) == total
    return value


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("/", "/"),
        ("/home/user/ComfyUI/user/__h3_context", "/home/user/ComfyUI/user/__h3_context"),
        ("/srv/comfy/output/", "/srv/comfy/output"),
        ("/data//comfy/./user", "/data/comfy/user"),
        ("/opt/Comfy UI/user", "/opt/Comfy UI/user"),
        ("/srv/漫畫/user", "/srv/漫畫/user"),
        ("/srv/back\\slash/user", "/srv/back\\slash/user"),
        ('/srv/"quoted"%*?<>|/user', '/srv/"quoted"%*?<>|/user'),
        ("/srv/" + "b" * MAX_POSIX_SEGMENT_BYTES, "/srv/" + "b" * MAX_POSIX_SEGMENT_BYTES),
    ),
)
def test_posix_grammar_admits_and_normalizes_absolute_directories(
    value: str, expected: str
) -> None:
    assert lexical_posix_directory(value) == PurePosixPath(expected)


@pytest.mark.parametrize(
    "value",
    (
        "",
        "relative/user",
        "./user",
        "~/ComfyUI/user",
        "//server/share/user",
        "/srv/../etc",
        "/srv/comfy/..",
        "/srv/comfy\x00/user",
        "/srv/comfy\n/user",
        "/srv/comfy\x7f/user",
        "/srv/\ud800/user",
        "/srv/" + "b" * (MAX_POSIX_SEGMENT_BYTES + 1),
        "/srv/" + "é" * 128,
        "C:\\ComfyUI\\user",
        "C:/ComfyUI/user",
    ),
)
def test_posix_grammar_refuses_non_locators(value: str) -> None:
    assert lexical_posix_directory(value) is None


@pytest.mark.parametrize("value", (None, 7, b"/srv/user", PurePosixPath("/srv/user")))
def test_posix_grammar_refuses_non_string_values(value: object) -> None:
    assert lexical_posix_directory(value) is None


def test_posix_grammar_enforces_the_whole_path_byte_bound() -> None:
    assert lexical_posix_directory(_exact_bytes_path(MAX_POSIX_LOCATOR_BYTES)) is not None
    assert lexical_posix_directory(_exact_bytes_path(MAX_POSIX_LOCATOR_BYTES + 1)) is None


_SEGMENT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters="/",
    ),
    min_size=1,
    max_size=24,
).filter(lambda segment: segment not in {".", ".."})


@given(st.lists(_SEGMENT, max_size=8))
def test_posix_grammar_keeps_ordinary_segments_and_is_idempotent(segments: list[str]) -> None:
    value = "/" + "/".join(segments)
    result = lexical_posix_directory(value)
    assert result == PurePosixPath(value)
    assert lexical_posix_directory(str(result)) == result
    # Repeated separators and `.` segments are spelling, not structure.
    assert lexical_posix_directory("/./" + "//".join(segments) + "/") == result


@given(st.lists(_SEGMENT, max_size=6), st.integers(min_value=0, max_value=6))
def test_posix_grammar_never_admits_a_parent_segment(segments: list[str], index: int) -> None:
    position = min(index, len(segments))
    with_parent = [*segments[:position], "..", *segments[position:]]
    assert lexical_posix_directory("/" + "/".join(with_parent)) is None


def test_host_locator_dispatches_on_the_platform_grammar() -> None:
    posix = "/home/user/ComfyUI/user/__h3_context"
    windows = "C:\\ComfyUI\\user\\__h3_context"
    assert _host_locator(posix, os_name="posix").as_posix() == posix
    assert PureWindowsPath(str(_host_locator(windows, os_name="nt"))) == PureWindowsPath(windows)
    # The Windows grammar refusing POSIX roots was defect D1; it must stay a per-platform choice.
    with pytest.raises(HostRootError) as refused:
        _host_locator(posix, os_name="nt")
    assert refused.value.reason is ResolutionReason.PRIVATE_ROOT_INVALID
    with pytest.raises(HostRootError) as windows_on_posix:
        _host_locator(windows, os_name="posix")
    assert windows_on_posix.value.reason is ResolutionReason.PRIVATE_ROOT_INVALID
    with pytest.raises(HostRootError) as unknown:
        _host_locator(posix, os_name="java")
    assert unknown.value.reason is ResolutionReason.UNSUPPORTED_HOST


def _posix_folder_paths(base: str) -> SimpleNamespace:
    return SimpleNamespace(
        get_input_directory=lambda: base + "/input",
        get_output_directory=lambda: base + "/output",
        get_temp_directory=lambda: base + "/temp",
        get_system_user_directory=lambda name: base + "/user/__" + name,
    )


def test_port_admits_posix_host_roots_with_the_posix_grammar() -> None:
    port = ComfyHostRootPort(
        module_provider=lambda: _posix_folder_paths("/srv/comfy"), os_name="posix"
    )
    assert port.private_root().as_posix() == "/srv/comfy/user/__" + PRIVATE_ROOT_NAME
    assert [root.as_posix() for root in port.served_roots()] == [
        "/srv/comfy/input",
        "/srv/comfy/output",
        "/srv/comfy/temp",
    ]


def test_port_normalizes_posix_served_roots_like_windows_ones() -> None:
    host = _posix_folder_paths("/srv/comfy")
    host.get_output_directory = lambda: "/srv/comfy/models/../output/"
    port = ComfyHostRootPort(module_provider=lambda: host, os_name="posix")
    assert port.served_roots()[1].as_posix() == "/srv/comfy/output"


def test_port_refuses_posix_roots_under_the_windows_grammar() -> None:
    port = ComfyHostRootPort(
        module_provider=lambda: _posix_folder_paths("/srv/comfy"), os_name="nt"
    )
    with pytest.raises(HostRootError) as refused:
        port.private_root()
    assert refused.value.reason is ResolutionReason.PRIVATE_ROOT_INVALID


def test_port_defaults_to_the_running_platform() -> None:
    assert ComfyHostRootPort()._os_name == os.name


@pytest.mark.parametrize(
    ("left", "right", "overlap"),
    (
        ("/srv/comfy/user/__h3_context", "/srv/comfy/output", False),
        ("/srv/comfy/output/__h3_context", "/srv/comfy/output", True),
        ("/srv/comfy", "/srv/comfy/temp", True),
        ("/srv/comfy/temp", "/srv/comfy/temp", True),
        ("/srv/comfy/outputs", "/srv/comfy/output", False),
        ("/", "/srv/comfy/input", True),
        # Case folding can only add refusals on a case-sensitive filesystem, never miss an overlap.
        ("/srv/Comfy/Output/__h3_context", "/srv/comfy/output", True),
    ),
)
def test_overlap_check_is_conservative_for_posix_roots(
    left: str, right: str, overlap: bool
) -> None:
    assert _overlaps(Path(left), Path(right)) is overlap
    assert _overlaps(Path(right), Path(left)) is overlap


# --------------------------------------------------------------------------------------------
# The App Mode closure through the real port


@pytest.fixture(params=("default", "tmpfs"))
def host_base(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Path]:
    if request.param == "default":
        yield tmp_path
        return
    # The row needs the real tmpfs mount; mkdtemp below creates a unique 0700 directory in it.
    shm = Path("/dev/shm")  # noqa: S108
    if not sys.platform.startswith("linux") or not shm.is_dir():
        pytest.skip("the tmpfs row needs a Linux host with /dev/shm")
    base = Path(tempfile.mkdtemp(prefix="h3-m23-67-", dir=shm))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _host_folder_paths(base: Path, *, private: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_input_directory=lambda: str(base / "input"),
        get_output_directory=lambda: str(base / "output"),
        get_temp_directory=lambda: str(base / "temp"),
        get_system_user_directory=lambda name: str(
            base / "user" / f"__{name}" if private is None else private
        ),
    )


def _real_port_coordinator(
    production_registry: ProductionWorkspaceRegistry,
) -> SequenceCoordinatorRegistry:
    return SequenceCoordinatorRegistry(
        production_registry=production_registry,
        output_root_factory=coordinator_adapter._host_output_root,
        private_root_factory=coordinator_adapter._host_private_root,
        artifact_store_factory=coordinator_adapter._host_artifact_store,
        artifact_inspector=lambda payload, **expected: ObservedVideoArtifact(
            "mp4", (192, 512, 512, 3)
        ),
        clock=lambda: 100.0,
        clock_ms=lambda: 100_000,
        token_factory=lambda: "m" * 40,
    )


def _closure(
    base: Path, monkeypatch: pytest.MonkeyPatch, *, private: Path | None = None
) -> tuple[SequenceCoordinatorRegistry, dict[str, object], Path, bytes]:
    for name in ("input", "output", "temp"):
        (base / name).mkdir()
    monkeypatch.setitem(sys.modules, "folder_paths", _host_folder_paths(base, private=private))
    artifact = base / "output" / "managed.mp4"
    payload = b"synthetic-video-payload"
    artifact.write_bytes(payload)
    production_registry, production = _production_workspace()
    coordinator = _real_port_coordinator(production_registry)
    prepared = _prepare(coordinator, production)
    submitted = _submit(coordinator, prepared)
    request = _action(
        "coordinator.artifact.real-port",
        "record_artifact",
        {
            "run_handle": prepared.run_handle,
            "expected_state_fingerprint": submitted.sequence.state.fingerprint,
            "queue_prompt_id": "prompt.model.1",
            "output_node_id": "node.save.video",
            "locator": {"filename": artifact.name, "subfolder": "", "type": "output"},
        },
    )
    return coordinator, request, artifact, payload


def test_app_mode_closure_stores_the_verified_copy_through_the_real_host_root_port(
    host_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, request, artifact, payload = _closure(host_base, monkeypatch)

    admitted = _required_response(coordinator.dispatch(request))

    assert admitted.disposition == "artifact_verified"
    selected = coordinator_adapter._host_private_root()
    assert selected.is_relative_to(host_base / "user" / f"__{PRIVATE_ROOT_NAME}")
    stored = [path for path in (selected / "artifacts").iterdir() if path.is_file()]
    assert [path.read_bytes() for path in stored] == [payload]
    # The verified copy is private; the host's own output file is left exactly as it was.
    assert artifact.read_bytes() == payload


def test_app_mode_closure_reports_a_refused_root_as_unavailable_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, request, artifact, payload = _closure(
        tmp_path, monkeypatch, private=tmp_path / "output" / "__h3_context"
    )

    with pytest.raises(SequenceCoordinatorError) as refused:
        coordinator.dispatch(request)

    # D4: a refused root is a storage failure, and retrying cannot heal it.
    assert (refused.value.code, refused.value.status) == ("host_storage_unavailable", 503)
    wire = refused.value.to_wire()
    assert wire["category"] == "artifact_store_unavailable"
    assert wire["retry_disposition"] == "use_native"
    assert artifact.read_bytes() == payload
    assert not (tmp_path / "output" / "__h3_context").exists()
