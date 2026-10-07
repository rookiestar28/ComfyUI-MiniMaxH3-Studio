from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import comfyui_h3_context.adapters.comfyui_media_runtime as runtime_module
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.media_runtime_manager import MediaRuntimeManager
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MediaRuntimeResolution,
    ResolutionReason,
    ResolutionState,
    SourceKind,
)
from comfyui_h3_context.adapters.segment_artifact_store import (
    PrivateSegmentArtifactStore,
)

MISSING = MediaRuntimeResolution(
    ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING
)


def located(root: Path, *, marker: int = 1) -> MediaRuntimeResolution:
    return MediaRuntimeResolution(
        ResolutionState.LOCATED,
        ResolutionReason.PAIR_ADMITTED,
        SourceKind.MANAGED,
        ffmpeg_path=(root / "bin" / "ffmpeg.exe").resolve(),
        ffprobe_path=(root / "bin" / "ffprobe.exe").resolve(),
        scratch_root=(root / "scratch").resolve(),
        ffmpeg_identity=(1, marker, 3, 4),
        ffprobe_identity=(1, marker, 3, 5),
    )


class Resolver:
    def __init__(self, result: MediaRuntimeResolution) -> None:
        self.result = result
        self.calls = 0

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        self.calls += 1
        return self.result

    def invalidate(self) -> None:
        return None

    def peek(self) -> MediaRuntimeResolution | None:
        return self.result


class Publisher:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish(self, adapter: object) -> None:
        self.published.append(adapter)

    def clear(self, adapter: object) -> None:
        self.published.remove(adapter)


def manager_for(
    resolver: Resolver, constructed: list[tuple[Path, Path, Path]], started: list[object]
) -> MediaRuntimeManager:
    def factory(ffmpeg: Path, ffprobe: Path, scratch: Path) -> QualifiedAVMediaAdapter:
        constructed.append((ffmpeg, ffprobe, scratch))
        return object.__new__(QualifiedAVMediaAdapter)

    return MediaRuntimeManager(
        resolver=resolver,  # type: ignore[arg-type]
        adapter_factory=factory,
        publisher=Publisher(),
        thread_starter=started.append,
    )


@pytest.fixture
def installed() -> Iterator[Callable[[MediaRuntimeManager], None]]:
    previous = composition_root.installed(composition_root.MEDIA_RUNTIME)

    def install(manager: MediaRuntimeManager) -> None:
        composition_root.install(composition_root.MEDIA_RUNTIME, manager)

    yield install
    if previous is None:
        composition_root.reset(composition_root.MEDIA_RUNTIME)
    else:
        composition_root.install(composition_root.MEDIA_RUNTIME, previous)


def test_no_located_pair_constructs_and_publishes_nothing(
    installed: Callable[[MediaRuntimeManager], None],
) -> None:
    constructed: list[tuple[Path, Path, Path]] = []
    started: list[object] = []
    manager = manager_for(Resolver(MISSING), constructed, started)
    installed(manager)

    assert runtime_module.current_authorized_media_runtime() is None
    assert constructed == []
    assert manager.current() is None


def test_first_use_activates_once_and_facades_share_the_adapter(
    tmp_path: Path, installed: Callable[[MediaRuntimeManager], None]
) -> None:
    constructed: list[tuple[Path, Path, Path]] = []
    resolver = Resolver(located(tmp_path))
    manager = manager_for(resolver, constructed, [])
    installed(manager)

    first = runtime_module.current_authorized_media_runtime()
    second = runtime_module.current_authorized_media_runtime()

    assert first is not None and first is second
    assert len(constructed) == 1
    assert constructed[0] == (
        (tmp_path / "bin" / "ffmpeg.exe").resolve(),
        (tmp_path / "bin" / "ffprobe.exe").resolve(),
        (tmp_path / "scratch").resolve(),
    )
    assert resolver.calls == 1


def test_m26_runtime_without_a_binding_asks_for_activation_and_builds_no_store(
    tmp_path: Path, installed: Callable[[MediaRuntimeManager], None]
) -> None:
    started: list[object] = []
    manager = manager_for(Resolver(located(tmp_path)), [], started)
    installed(manager)
    artifact_store = PrivateSegmentArtifactStore(tmp_path / "artifacts", clock_ms=lambda: 100)

    assert runtime_module.authorized_m26_assembly_runtime(artifact_store) is None
    assert runtime_module.authorized_authoring_derivative_runtime() is None
    assert len(started) == 1
    assert not (tmp_path / "scratch" / "m26-production-reconstruction").exists()


def test_m26_runtime_reuses_one_single_writer_store_from_the_binding(
    tmp_path: Path, installed: Callable[[MediaRuntimeManager], None]
) -> None:
    manager = manager_for(Resolver(located(tmp_path)), [], [])
    installed(manager)
    binding = manager.ensure()
    assert binding is not None
    artifact_store = PrivateSegmentArtifactStore(tmp_path / "artifacts", clock_ms=lambda: 100)

    first = runtime_module.authorized_m26_assembly_runtime(artifact_store)
    second = runtime_module.authorized_m26_assembly_runtime(artifact_store)

    assert first is not None and second is not None
    assert first.artifact_store is artifact_store
    assert first.media_bridge is binding.adapter
    assert first.low_level_adapter is binding.adapter
    assert first.reconstruction_store is second.reconstruction_store
    assert first.reconstruction_store._policy.max_concurrent_writes == 1
    assert first.reconstruction_store._root == (
        (tmp_path / "scratch").resolve() / "m26-production-reconstruction"
    )
    assert first.capability.max_workers == 1
    serialized = str(first.capability.to_wire()).lower()
    assert "scratch" not in serialized
    assert "ffmpeg.exe" not in serialized
    assert "ffprobe.exe" not in serialized


def test_the_derivative_generator_is_scoped_to_one_binding(
    tmp_path: Path,
    installed: Callable[[MediaRuntimeManager], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import authoring_derivative_generator

    built: list[dict[str, object]] = []

    def generator(**kwargs: object) -> object:
        built.append(kwargs)
        return object()

    monkeypatch.setattr(authoring_derivative_generator, "AuthoringDerivativeGenerator", generator)
    manager = manager_for(Resolver(located(tmp_path)), [], [])
    installed(manager)
    assert manager.ensure() is not None

    first = runtime_module.authorized_authoring_derivative_runtime()
    second = runtime_module.authorized_authoring_derivative_runtime()

    assert first is not None and first is second
    assert built == [
        {
            "ffmpeg_path": (tmp_path / "bin" / "ffmpeg.exe").resolve(),
            "ffprobe_path": (tmp_path / "bin" / "ffprobe.exe").resolve(),
            "scratch_root": (tmp_path / "scratch").resolve() / "authoring-derivatives",
        }
    ]
