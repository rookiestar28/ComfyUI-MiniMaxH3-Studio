"""M25-32: one in-process media runtime binding, activated on first use and refreshed in place.

The rows use a fake resolver, adapter factory, publisher and consumers so every transition is
deterministic; the native absent-to-ready row runs only with the exact pinned pair supplied.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import comfyui_h3_context.adapters.comfyui_authoring_output_runtime as output_module
import comfyui_h3_context.adapters.comfyui_media_runtime as facades
from comfyui_h3_context.adapters import composition_root
from comfyui_h3_context.adapters.av_reconstruction_media import QualifiedAVMediaAdapter
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (
    AuthoringWorkbenchError,
    AuthoringWorkspaceRegistry,
)
from comfyui_h3_context.adapters.comfyui_media_runtime_setup import (
    STATUS_SCHEMA,
    MediaRuntimeSetupError,
    MediaRuntimeSetupService,
)
from comfyui_h3_context.adapters.comfyui_production_workspace import ProductionWorkspaceRegistry
from comfyui_h3_context.adapters.media_runtime_manager import (
    FEATURES,
    MediaRuntimeBinding,
    MediaRuntimeBusy,
    MediaRuntimeManager,
    binding_identity,
)
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MediaRuntimeConfig,
    MediaRuntimeResolution,
    MediaRuntimeSelection,
    ResolutionReason,
    ResolutionState,
    SourceKind,
    private_layout,
)
from comfyui_h3_context.adapters.production_authoring_import_service import (
    ProductionAuthoringImportService,
)
from comfyui_h3_context.adapters.segment_artifact_store import PrivateSegmentArtifactStore

REPO = Path(__file__).resolve().parents[1]
MISSING = MediaRuntimeResolution(
    ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING
)
DISCOVERING = MediaRuntimeResolution(
    ResolutionState.DISCOVERING, ResolutionReason.DISCOVERY_IN_PROGRESS
)


def located(
    root: Path, *, marker: int = 1, kind: SourceKind = SourceKind.MANAGED
) -> MediaRuntimeResolution:
    return MediaRuntimeResolution(
        ResolutionState.LOCATED,
        ResolutionReason.PAIR_ADMITTED,
        kind,
        ffmpeg_path=(root / f"bin{marker}" / "ffmpeg.exe").resolve(),
        ffprobe_path=(root / f"bin{marker}" / "ffprobe.exe").resolve(),
        scratch_root=(root / "scratch").resolve(),
        ffmpeg_identity=(1, marker, 3, 4),
        ffprobe_identity=(1, marker, 3, 5),
    )


class Resolver:
    """Returns `result`; `peek` sees it only after a resolve, like the real cache."""

    def __init__(self, result: MediaRuntimeResolution) -> None:
        self.result = result
        self.resolved = False
        self.calls = 0
        self.rescans = 0
        self.gate: threading.Event | None = None
        self.config: MediaRuntimeConfig | None = None
        self.writes: list[MediaRuntimeSelection] = []

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        self.calls += 1
        self.rescans += int(rescan)
        if self.gate is not None:
            assert self.gate.wait(10)
        self.resolved = True
        return self.result

    def invalidate(self) -> None:
        self.resolved = False

    def peek(self) -> MediaRuntimeResolution | None:
        return self.result if self.resolved else None

    def read_config(self) -> MediaRuntimeConfig | None:
        return self.config

    def write_config(
        self,
        *,
        expected_revision: int,
        selection: MediaRuntimeSelection,
        directory: str | None = None,
    ) -> MediaRuntimeConfig:
        self.writes.append(selection)
        self.config = MediaRuntimeConfig(expected_revision + 1, selection, directory)
        return self.config

    def private_layout(self) -> object:
        return private_layout(Path("C:/private"))


class Publisher:
    def __init__(self) -> None:
        self.published: list[object] = []
        self.events: list[str] = []
        self.fail = False

    def publish(self, adapter: object) -> None:
        if self.fail:
            raise RuntimeError("publication refused")
        self.published.append(adapter)
        self.events.append("publish")

    def clear(self, adapter: object) -> None:
        self.published.remove(adapter)
        self.events.append("clear")


class Consumer:
    def __init__(self, feature: str = "render", *, ready_on_attach: bool = True) -> None:
        self.feature = feature
        self.ready_on_attach = ready_on_attach
        self.attached: list[MediaRuntimeBinding] = []
        self.detached = 0
        self.is_busy = False
        self.fail = False
        self._ready = False

    def attach(self, binding: MediaRuntimeBinding) -> None:
        if self.fail:
            raise RuntimeError("attach refused")
        self.attached.append(binding)
        self._ready = self.ready_on_attach

    def detach(self) -> None:
        self.detached += 1
        self._ready = False

    def busy(self) -> bool:
        return self.is_busy

    def ready(self) -> bool:
        return self._ready


class Harness:
    def __init__(self, resolution: MediaRuntimeResolution, **kwargs: Any) -> None:
        self.resolver = Resolver(resolution)
        self.publisher = Publisher()
        self.constructed: list[tuple[Path, Path, Path]] = []
        self.started: list[Callable[[], None]] = []
        self.factory_error: Exception | None = None
        self.manager = MediaRuntimeManager(
            resolver=self.resolver,  # type: ignore[arg-type]
            adapter_factory=self.factory,
            publisher=self.publisher,
            thread_starter=kwargs.pop("thread_starter", self.started.append),
            **kwargs,
        )

    def factory(self, ffmpeg: Path, ffprobe: Path, scratch: Path) -> QualifiedAVMediaAdapter:
        if self.factory_error is not None:
            raise self.factory_error
        self.constructed.append((ffmpeg, ffprobe, scratch))
        return object.__new__(QualifiedAVMediaAdapter)

    def run_started(self) -> None:
        while self.started:
            self.started.pop(0)()


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


# -- AC32-01: inert import and construction --------------------------------------------------


def test_package_import_performs_no_activation_even_with_the_legacy_override(
    tmp_path: Path,
) -> None:
    probe = r"""
import json, subprocess, threading
spawned = []
original = subprocess.Popen.__init__
def spy(self, *args, **kwargs):
    spawned.append(1)
    return original(self, *args, **kwargs)
subprocess.Popen.__init__ = spy
import comfyui_h3_context
import comfyui_h3_context.adapters.comfyui_media_runtime as facades
from comfyui_h3_context.adapters import composition_root as root
from comfyui_h3_context.adapters.comfyui_authoring_media_preview import (
    current_authoring_media_preview_adapter,
)
print(json.dumps({
    "spawned": len(spawned),
    "manager": root.installed(root.MEDIA_RUNTIME) is not None,
    "resolver": root.installed(root.MEDIA_RUNTIME_RESOLVER) is not None,
    "adapter": current_authoring_media_preview_adapter() is not None,
    "threads": sorted(t.name for t in threading.enumerate() if t.name.startswith("h3-")),
    "configure": hasattr(facades, "configure_authorized_media_runtime"),
}))
"""
    environment = {
        **os.environ,
        "H3_CONTEXT_AUTHORIZED_MEDIA_RUNTIME": "1",
        "H3_CONTEXT_AUTHORIZED_FFMPEG_PATH": str(tmp_path / "ffmpeg.exe"),
        "H3_CONTEXT_AUTHORIZED_FFPROBE_PATH": str(tmp_path / "ffprobe.exe"),
        "H3_CONTEXT_AUTHORIZED_MEDIA_SCRATCH_ROOT": str(tmp_path / "scratch"),
    }
    completed = subprocess.run(  # noqa: S603 - the current interpreter on a fixed probe
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "spawned": 0,
        "manager": False,
        "resolver": False,
        "adapter": False,
        "threads": [],
        "configure": False,
    }


def test_the_output_runtime_and_package_entry_hold_no_environment_reader() -> None:
    output_tree = ast.parse(Path(output_module.__file__).read_text(encoding="utf-8"))
    names = {node.attr for node in ast.walk(output_tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(output_tree) if isinstance(node, ast.Name)
    }
    assert "environ" not in names and "getenv" not in names
    entry = ast.parse((REPO / "comfyui_h3_context" / "__init__.py").read_text(encoding="utf-8"))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
    }
    assert "configure_authorized_media_runtime" not in called


def test_manager_construction_is_inert(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))

    assert harness.resolver.calls == 0
    assert harness.started == []
    assert harness.constructed == []
    assert harness.publisher.events == []
    assert harness.manager.current() is None
    assert harness.resolver.calls == 0
    assert repr(harness.manager) == "<MediaRuntimeManager>"


# -- AC32-02: first use activates once ------------------------------------------------------


def test_concurrent_first_use_coalesces_to_one_construction(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.resolver.gate = threading.Event()
    results: list[MediaRuntimeBinding | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(harness.manager.ensure())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    harness.resolver.gate.set()
    for thread in threads:
        thread.join(10)

    assert len(results) == 8
    assert len({id(binding) for binding in results}) == 1
    assert results[0] is not None
    assert len(harness.constructed) == 1
    assert harness.resolver.calls == 1
    assert harness.publisher.published == [results[0].adapter]


def test_event_loop_readers_start_at_most_one_activation_and_never_resolve(
    tmp_path: Path,
) -> None:
    harness = Harness(located(tmp_path))

    for _ in range(5):
        assert harness.manager.current() is None
        harness.manager.request_activation()
        harness.manager.readiness()
    assert len(harness.started) == 1
    assert harness.resolver.calls == 0

    harness.run_started()
    binding = harness.manager.current()
    assert binding is not None
    harness.manager.request_activation()
    assert harness.started == []
    assert len(harness.constructed) == 1


def test_a_missing_pair_or_failed_activation_is_not_retried_per_poll(tmp_path: Path) -> None:
    missing = Harness(MISSING)
    missing.manager.request_activation()
    missing.run_started()
    for _ in range(3):
        missing.manager.request_activation()
        missing.manager.readiness()
    assert missing.started == []
    assert missing.resolver.calls == 1

    failing = Harness(located(tmp_path))
    failing.factory_error = OSError("pin refused")
    failing.manager.request_activation()
    failing.run_started()
    failing.manager.request_activation()
    assert failing.started == []
    # A worker-thread request still retries; only event-loop polls are suppressed.
    failing.factory_error = None
    assert failing.manager.ensure() is not None


# -- AC32-03: cold absent-to-ready in one process --------------------------------------------


def test_cold_absent_to_ready_keeps_the_same_consumers_and_composition(
    tmp_path: Path,
    installed: Callable[[MediaRuntimeManager], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(MISSING)
    installed(harness.manager)

    class Registry:
        busy = False
        closed = 0

        def close(self) -> None:
            Registry.closed += 1

    registries: list[Registry] = []

    def build(binding: MediaRuntimeBinding, workspace: object) -> Registry:
        registries.append(Registry())
        return registries[-1]

    monkeypatch.setattr(output_module, "_build_registry", build)
    output = output_module.build_authoring_output_runtime()
    production = ProductionWorkspaceRegistry(seed_claim=cast(Any, lambda _handle: None))
    authoring = AuthoringWorkspaceRegistry(workspace_claim=cast(Any, lambda _handle: None))
    service = ProductionAuthoringImportService(
        production_registry=production, authoring_registry=authoring
    )
    imported: list[object] = []

    def import_outputs(_request: object, **kwargs: object) -> str:
        imported.append(kwargs["media_adapter"])
        return "imported"

    monkeypatch.setattr(authoring, "import_production_outputs", import_outputs)
    try:
        assert output.capability()["supported"] is False
        with pytest.raises(AuthoringWorkbenchError) as refused:
            service.dispatch(cast(Any, object()), deadline=time.monotonic() + 5)
        assert (refused.value.status, refused.value.code) == (503, "source_unavailable")
        assert facades.current_authorized_media_runtime() is None
        assert harness.manager.readiness()["import"]["state"] == "setup_required"

        harness.resolver.result = located(tmp_path)
        binding = harness.manager.refresh()

        assert binding is not None
        assert composition_root.installed(composition_root.MEDIA_RUNTIME) is harness.manager
        assert output.capability()["supported"] is True
        assert output.registry() is cast(Any, registries[0])
        assert (
            cast(Any, service.dispatch(cast(Any, object()), deadline=time.monotonic() + 5))
            == "imported"
        )
        assert imported == [binding.adapter]
        assert facades.current_authorized_media_runtime() is binding.adapter
        assert harness.manager.readiness() == {
            feature: {"state": "ready", "reason": None} for feature in FEATURES
        }
    finally:
        harness.manager.remove_consumer(output)
        output.close()


def test_the_setup_install_job_activates_through_refresh(tmp_path: Path) -> None:
    harness = Harness(MISSING)
    consumer = Consumer()
    harness.manager.add_consumer(consumer)

    class Installer:
        def run(self) -> None:
            harness.resolver.result = located(tmp_path)

    setup = MediaRuntimeSetupService(
        resolver=harness.resolver,  # type: ignore[arg-type]
        manager=harness.manager,
        downloader_factory=lambda: cast(Any, object()),
        installer_factory=lambda _layout, **_kwargs: cast(Any, Installer()),
        manifest_provider=lambda: cast(
            Any,
            type(
                "Manifest",
                (),
                {
                    "profile_component": "p",
                    "source_label": "s",
                    "release_page_url": "https://example.invalid/",
                    "license_name": "l",
                    "archive_bytes": 1,
                },
            )(),
        ),
        thread_starter=lambda target: target(),
        job_id_factory=lambda: "a" * 32,
    )

    job = setup.start_install()

    assert setup.job(cast(str, job["job_id"]))["reason"] == "installed"
    binding = harness.manager.current()
    assert binding is not None and binding.source_kind is SourceKind.MANAGED
    assert consumer.attached == [binding]
    assert harness.resolver.rescans >= 1


# -- AC32-04: one binding for every consumer -------------------------------------------------


def test_every_consumer_derives_from_one_binding_and_reapplying_it_is_a_no_op(
    tmp_path: Path,
    installed: Callable[[MediaRuntimeManager], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import authoring_derivative_generator

    monkeypatch.setattr(
        authoring_derivative_generator, "AuthoringDerivativeGenerator", lambda **_kw: object()
    )
    harness = Harness(located(tmp_path))
    installed(harness.manager)
    consumer = Consumer()
    harness.manager.add_consumer(consumer)
    binding = harness.manager.ensure()
    assert binding is not None
    artifact_store = PrivateSegmentArtifactStore(tmp_path / "artifacts", clock_ms=lambda: 100)
    generator = facades.authorized_authoring_derivative_runtime()
    runtime = facades.authorized_m26_assembly_runtime(artifact_store)
    assert runtime is not None

    again = harness.manager.refresh()

    assert again is binding
    assert harness.publisher.published == [binding.adapter]
    assert runtime.media_bridge is binding.adapter
    assert facades.current_authorized_media_runtime() is binding.adapter
    assert consumer.attached == [binding] and consumer.detached == 0
    assert len(harness.constructed) == 1
    assert facades.authorized_authoring_derivative_runtime() is generator
    later = facades.authorized_m26_assembly_runtime(artifact_store)
    assert later is not None and later.reconstruction_store is runtime.reconstruction_store
    assert binding.identity == binding_identity(located(tmp_path))
    assert str(tmp_path) not in repr(binding)


def test_real_sized_file_identities_activate_and_distinguish_bindings(tmp_path: Path) -> None:
    # NTFS volume serials, 64-bit file indexes and ns mtimes exceed the canonical integer range.
    base = located(tmp_path)
    wide = MediaRuntimeResolution(
        base.state,
        base.reason,
        base.source_kind,
        ffmpeg_path=base.ffmpeg_path,
        ffprobe_path=base.ffprobe_path,
        scratch_root=base.scratch_root,
        ffmpeg_identity=(13_133_731_871_708_106_597, 2**63 + 5, 204_800_000, 1_789_397_077 * 10**9),
        ffprobe_identity=(
            13_133_731_871_708_106_597,
            2**63 + 6,
            204_700_000,
            1_789_397_077 * 10**9,
        ),
    )
    harness = Harness(wide)

    binding = harness.manager.ensure()

    assert binding is not None and binding.identity == binding_identity(wide)
    assert binding.identity != binding_identity(base)


def test_a_superseded_scan_keeps_the_active_binding(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    binding = harness.manager.ensure()
    harness.resolver.result = DISCOVERING

    assert harness.manager.refresh() is binding
    assert harness.manager.current() is binding
    assert harness.publisher.events == ["publish"]


# -- AC32-05: lease boundary -----------------------------------------------------------------


@pytest.mark.parametrize("holder", ["lease", "consumer", "store", "probe"])
def test_refresh_refuses_while_media_work_is_held_and_leaves_config_untouched(
    tmp_path: Path, holder: str
) -> None:
    held = {"probe": False}
    harness = Harness(located(tmp_path), busy_probes=(lambda: held["probe"],))
    consumer = Consumer()
    harness.manager.add_consumer(consumer)
    binding = harness.manager.ensure()
    assert binding is not None
    writes: list[str] = []
    harness.resolver.result = located(tmp_path, marker=2)

    class Store:
        active_execution_count = 1

    lease = harness.manager.lease()
    if holder == "lease":
        lease.__enter__()
    elif holder == "consumer":
        consumer.is_busy = True
    elif holder == "store":
        harness.manager.store("root", Store)
    else:
        held["probe"] = True
    try:
        with pytest.raises(MediaRuntimeBusy):
            harness.manager.refresh(transition=lambda: writes.append("written"))
    finally:
        if holder == "lease":
            lease.__exit__(None, None, None)

    assert writes == []
    assert harness.manager.current() is binding
    assert consumer.detached == 0
    assert harness.publisher.events == ["publish"]


def test_absent_to_ready_is_never_refused_as_busy(tmp_path: Path) -> None:
    harness = Harness(MISSING, busy_probes=(lambda: True,))
    with harness.manager.lease():
        harness.resolver.result = located(tmp_path)
        assert harness.manager.refresh() is not None


def test_a_lease_requested_during_a_transition_fails_fast(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.manager.ensure()
    outcomes: list[str] = []

    def transition() -> None:
        def other() -> None:
            try:
                with harness.manager.lease():
                    outcomes.append("leased")
            except MediaRuntimeBusy:
                outcomes.append("busy")

        worker = threading.Thread(target=other)
        worker.start()
        worker.join(5)

    harness.manager.refresh(transition=transition)

    assert outcomes == ["busy"]
    with harness.manager.lease():
        outcomes.append("leased")
    assert outcomes == ["busy", "leased"]


def test_idle_replacement_detaches_then_attaches_the_new_binding(
    tmp_path: Path,
    installed: Callable[[MediaRuntimeManager], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfyui_h3_context.adapters import authoring_derivative_generator

    monkeypatch.setattr(
        authoring_derivative_generator, "AuthoringDerivativeGenerator", lambda **_kw: object()
    )
    harness = Harness(located(tmp_path))
    installed(harness.manager)
    consumer = Consumer()
    harness.manager.add_consumer(consumer)
    first = harness.manager.ensure()
    assert first is not None
    generator = facades.authorized_authoring_derivative_runtime()
    store = harness.manager.store("root", object)

    harness.resolver.result = located(tmp_path, marker=2, kind=SourceKind.LOCAL_SELECTION)
    second = harness.manager.refresh()

    assert second is not None and second is not first
    assert second.identity != first.identity
    assert consumer.detached == 1
    assert consumer.attached == [first, second]
    assert harness.publisher.events == ["publish", "clear", "publish"]
    assert harness.publisher.published == [second.adapter]
    assert facades.authorized_authoring_derivative_runtime() is not generator
    assert harness.manager.store("root", object) is store

    harness.resolver.result = MISSING
    assert harness.manager.refresh() is None
    assert harness.manager.current() is None
    assert consumer.detached == 2
    assert harness.publisher.published == []


def test_a_busy_local_selection_is_a_409_with_config_bytes_unchanged(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.manager.ensure()
    setup = MediaRuntimeSetupService(
        resolver=harness.resolver,  # type: ignore[arg-type]
        manager=harness.manager,
        thread_starter=lambda target: target(),
    )
    with harness.manager.lease(), pytest.raises(MediaRuntimeSetupError) as refused:
        setup.use_local_directory("C:\\tools", 0)

    assert (refused.value.code, refused.value.status) == ("media_runtime_busy", 409)
    assert harness.resolver.writes == []


# -- AC32-06: readiness ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolution", "resolved", "expected"),
    [
        (None, False, ("unavailable", "discovering")),
        ("located", False, ("unavailable", "discovering")),
        ("located", True, ("unavailable", "activating")),
        (MISSING, True, ("setup_required", "supported_pair_missing")),
        (
            MediaRuntimeResolution(
                ResolutionState.UNAVAILABLE,
                ResolutionReason.DISCOVERY_LIMIT,
                managed_searched=True,
            ),
            True,
            ("setup_required", "discovery_limit"),
        ),
        (
            MediaRuntimeResolution(ResolutionState.UNAVAILABLE, ResolutionReason.DISCOVERY_LIMIT),
            True,
            ("unavailable", "discovery_limit"),
        ),
        (
            MediaRuntimeResolution(
                ResolutionState.INVALID_CONFIG,
                ResolutionReason.CONFIG_CORRUPT,
                SourceKind.LOCAL_SELECTION,
            ),
            True,
            ("unavailable", "invalid_config"),
        ),
        (
            MediaRuntimeResolution(
                ResolutionState.UNAVAILABLE, ResolutionReason.UNSUPPORTED_PLATFORM
            ),
            True,
            ("unavailable", "unsupported_platform"),
        ),
    ],
)
def test_unbound_readiness_reports_one_closed_reason_for_every_feature(
    tmp_path: Path,
    resolution: MediaRuntimeResolution | str | None,
    resolved: bool,
    expected: tuple[str, str],
) -> None:
    if resolution is None or resolution == "located":
        actual = located(tmp_path)
    else:
        actual = cast(MediaRuntimeResolution, resolution)
    harness = Harness(actual)
    harness.resolver.resolved = resolved

    wire = harness.manager.readiness()

    state, reason = expected
    assert wire == {feature: {"state": state, "reason": reason} for feature in FEATURES}
    assert len(harness.started) == (1 if reason in {"discovering", "activating"} else 0)
    assert harness.constructed == []


def test_render_needs_its_qualified_consumer_while_other_features_are_ready(
    tmp_path: Path,
) -> None:
    harness = Harness(located(tmp_path))
    unqualified = Consumer(ready_on_attach=False)
    harness.manager.add_consumer(unqualified)
    harness.manager.ensure()

    wire = harness.manager.readiness()
    assert wire["render"] == {"state": "unavailable", "reason": "render_qualification_unavailable"}
    assert all(wire[feature]["state"] == "ready" for feature in FEATURES if feature != "render")

    qualified = Consumer()
    harness.manager.add_consumer(qualified)
    harness.run_started()
    assert harness.manager.readiness()["render"] == {"state": "ready", "reason": None}


def test_status_carries_features_and_no_locator(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.manager.add_consumer(Consumer())
    setup = MediaRuntimeSetupService(
        resolver=harness.resolver,  # type: ignore[arg-type]
        manager=harness.manager,
        thread_starter=lambda target: target(),
        parked_scanner=lambda _layout, _manifest: (),
    )

    status = setup.status()

    # M25-33 bumped the wire to v3 (adds `recovery`); the feature projection is unchanged.
    assert status["schema"] == STATUS_SCHEMA == "h3.context.media_runtime_status.v3"
    assert status["features"] == {
        feature: {"state": "ready", "reason": None} for feature in FEATURES
    }
    encoded = json.dumps(status)
    assert str(tmp_path) not in encoded and "ffmpeg.exe" not in encoded
    assert harness.manager.current() is not None


# -- AC32-07: activation failures ------------------------------------------------------------


def test_a_construction_failure_records_and_publishes_nothing(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.factory_error = OSError("pin refused")
    consumer = Consumer()
    harness.manager.add_consumer(consumer)

    assert harness.manager.ensure() is None
    assert harness.publisher.events == []
    assert consumer.attached == []
    assert harness.manager.readiness()["import"] == {
        "state": "unavailable",
        "reason": "activation_failed",
    }


def test_a_publication_failure_leaves_no_recorded_binding(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.publisher.fail = True
    consumer = Consumer()
    harness.manager.add_consumer(consumer)

    assert harness.manager.ensure() is None
    assert harness.manager.current() is None
    assert consumer.attached == []
    assert harness.manager.readiness()["preview"]["reason"] == "activation_failed"

    harness.publisher.fail = False
    binding = harness.manager.ensure()
    assert binding is not None and harness.publisher.published == [binding.adapter]


def test_a_failing_attach_withholds_render_only(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    consumer = Consumer()
    consumer.fail = True
    harness.manager.add_consumer(consumer)

    assert harness.manager.ensure() is not None
    wire = harness.manager.readiness()
    assert wire["render"]["reason"] == "render_qualification_unavailable"
    assert wire["import"] == {"state": "ready", "reason": None}


@pytest.mark.parametrize(
    "reason",
    [
        ResolutionReason.OVERRIDE_INCOMPLETE,
        ResolutionReason.OVERRIDE_INVALID_MARKER,
        ResolutionReason.OVERRIDE_UNSUPPORTED_PAIR,
    ],
)
def test_invalid_legacy_overrides_keep_their_reason_without_fallback(
    reason: ResolutionReason,
) -> None:
    resolution = MediaRuntimeResolution(
        ResolutionState.INVALID_CONFIG, reason, SourceKind.EXPLICIT_OVERRIDE
    )
    harness = Harness(resolution)

    assert harness.manager.ensure() is None
    assert harness.constructed == []
    assert harness.manager.readiness()["assembly"] == {
        "state": "unavailable",
        "reason": "invalid_config",
    }
    assert harness.manager.refresh() is None
    assert harness.publisher.events == []


# -- consumers built after activation, and the lease entry points ----------------------------


def test_a_consumer_composed_after_activation_attaches_off_the_calling_thread(
    tmp_path: Path,
) -> None:
    harness = Harness(located(tmp_path))
    binding = harness.manager.ensure()
    consumer = Consumer()

    harness.manager.add_consumer(consumer)
    assert consumer.attached == []
    harness.run_started()
    assert consumer.attached == [binding]


def test_a_late_attach_is_dropped_when_the_binding_changed(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    harness.manager.ensure()
    consumer = Consumer()
    harness.manager.add_consumer(consumer)
    late = harness.started.pop()
    harness.resolver.result = located(tmp_path, marker=2)
    second = harness.manager.refresh()

    late()

    assert consumer.attached == [second]


def test_current_never_waits_for_a_slow_scoped_build(tmp_path: Path) -> None:
    harness = Harness(located(tmp_path))
    binding = harness.manager.ensure()
    entered = threading.Event()
    release = threading.Event()

    def slow(_binding: MediaRuntimeBinding) -> object:
        entered.set()
        assert release.wait(10)
        return object()

    worker = threading.Thread(target=lambda: harness.manager.scoped("slow", slow))
    worker.start()
    assert entered.wait(5)
    started = time.monotonic()
    assert harness.manager.current() is binding
    assert time.monotonic() - started < 0.5
    release.set()
    worker.join(5)


def test_the_assembly_job_fails_fast_when_its_lease_is_refused() -> None:
    def refused() -> Any:
        raise MediaRuntimeBusy()

    registry = ProductionWorkspaceRegistry(
        seed_claim=cast(Any, lambda _handle: None), media_lease=refused
    )
    failures: list[str] = []

    class Cancellation:
        def is_cancelled(self) -> bool:
            return False

    job = type("Job", (), {"cancellation": Cancellation()})()
    patched = cast(Any, registry)
    patched._fail_assembly_job = lambda _h, _e, _j, code: failures.append(code)
    patched._run_leased_assembly_job = lambda *_args: failures.append("ran")

    registry._run_assembly_job("handle", cast(Any, object()), cast(Any, job))

    assert failures == ["assembly_worker_unavailable"]


def test_preview_execution_holds_a_lease_and_rechecks_the_published_adapter(
    tmp_path: Path,
    installed: Callable[[MediaRuntimeManager], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import comfyui_h3_context.adapters.comfyui_authoring_media_preview as preview
    from comfyui_h3_context.adapters.authoring_source_binding import AuthoringSourceBindingError

    harness = Harness(located(tmp_path))
    installed(harness.manager)
    binding = harness.manager.ensure()
    assert binding is not None
    seen: list[int] = []

    class Claim:
        def render(self, adapter: object, **_kwargs: object) -> tuple[bytes, str]:
            seen.append(harness.manager._state.leases)
            return b"body", "absent"

    monkeypatch.setattr(preview, "current_authoring_media_preview_adapter", lambda: binding.adapter)
    result = preview._render(
        Claim(), binding.adapter, time.monotonic() + 5, preview._Cancellation()
    )
    assert seen == [1]
    result.discard()

    other = object.__new__(QualifiedAVMediaAdapter)
    with pytest.raises(AuthoringSourceBindingError):
        preview._render(Claim(), other, time.monotonic() + 5, preview._Cancellation())
    assert seen == [1]


# -- AC32-08: native absent-to-ready ---------------------------------------------------------


def _pinned_pair() -> tuple[Path, Path] | None:
    ffmpeg = os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe = os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg or not ffprobe:
        return None
    return Path(ffmpeg), Path(ffprobe)


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@pytest.mark.skipif(_pinned_pair() is None, reason="the exact pinned media pair was not supplied")
def test_native_absent_to_ready_imports_previews_and_attaches_render_in_one_process(
    tmp_path: Path, installed: Callable[[MediaRuntimeManager], None]
) -> None:
    from test_m25_29_production_authoring_import import (
        _import_request,
        _registries_with_ready_output,
    )

    import comfyui_h3_context.adapters.comfyui_authoring_media_preview as preview

    pair = _pinned_pair()
    assert pair is not None
    ffmpeg, ffprobe = (value.resolve(strict=True) for value in pair)
    encoded = (tmp_path / "production-segment.mp4").resolve()
    subprocess.run(  # noqa: S603 - the explicitly supplied pinned encoder on a fixed argv
        [
            str(ffmpeg),
            *("-hide_banner", "-loglevel", "error", "-nostdin"),
            *("-f", "lavfi", "-i", "testsrc2=size=512x512:rate=24:duration=8"),
            *("-c:v", "libx264", "-bf", "0"),
            *("-vf", "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"),
            *("-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709"),
            *("-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart"),
            *("-y", str(encoded)),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    body = encoded.read_bytes()
    production, projection, authoring, before, history, _store = _registries_with_ready_output(
        tmp_path, artifact_bodies=(body,), frame_count=192, width=512, height=512
    )
    resolver = Resolver(MISSING)
    manager = MediaRuntimeManager(resolver=resolver)  # type: ignore[arg-type]
    installed(manager)
    output = output_module.build_authoring_output_runtime()
    service = ProductionAuthoringImportService(
        production_registry=production, authoring_registry=authoring
    )
    request = _import_request(projection, before, history)
    try:
        # Missing: the same service and registries refuse, and nothing is published.
        assert manager.ensure() is None
        assert output.capability()["supported"] is False
        with pytest.raises(AuthoringWorkbenchError) as refused:
            service.dispatch(request, deadline=time.monotonic() + 30)
        assert refused.value.code == "source_unavailable"
        assert preview.current_authoring_media_preview_adapter() is None

        resolver.result = MediaRuntimeResolution(
            ResolutionState.LOCATED,
            ResolutionReason.PAIR_ADMITTED,
            SourceKind.LOCAL_SELECTION,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            scratch_root=(tmp_path / "scratch").resolve(),
            ffmpeg_identity=_identity(ffmpeg),
            ffprobe_identity=_identity(ffprobe),
        )
        binding = manager.refresh()

        assert binding is not None
        assert preview.current_authoring_media_preview_adapter() is binding.adapter
        response = service.dispatch(request, deadline=time.monotonic() + 30)
        assert len(response.receipt.rows) == 1
        assert (
            binding.adapter.probe_authoring_source_duration(
                source_path=encoded, deadline=time.monotonic() + 30
            )
            == 8_000
        )

        class Claim:
            def render(
                self, adapter: QualifiedAVMediaAdapter, *, deadline: float, cancellation: object
            ) -> tuple[bytes | bytearray, str]:
                return adapter.execute_authoring_preview(
                    source_path=encoded,
                    source_start_frame=24,
                    frames=48,
                    source_fps=24,
                    deadline=deadline,
                )

        result = preview._render(
            Claim(), binding.adapter, time.monotonic() + 30, preview._Cancellation()
        )
        assert bytes(result.take()[4:8]) == b"ftyp"
        readiness = manager.readiness()
        assert all(readiness[feature]["state"] == "ready" for feature in FEATURES[:4])
        if sys.platform == "win32":
            assert output.capability()["supported"] is True
            assert readiness["render"] == {"state": "ready", "reason": None}
    finally:
        manager.remove_consumer(output)
        output.close()
        active = manager.current()
        if active is not None:
            manager._detach(active)
