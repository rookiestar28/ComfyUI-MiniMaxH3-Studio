"""M25-33: the media setup entry's backend half.

Status v3 adds `recovery` for a parked runtime tree and one explicit reclaim action. The browser
decoder refuses every value outside the closed wire vocabularies, so this module also pins those
vocabularies and representative wires in `frontend/tests/fixtures/media_runtime_wire_v3.json`,
which the frontend codec suite decodes: both sides read the same file.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import comfyui_h3_context.adapters.media_runtime_installer as installer_module
from comfyui_h3_context.adapters.comfyui_media_runtime_setup import (
    ACTIONS,
    FEATURE_REASONS,
    FEATURE_STATES,
    JOB_PHASES,
    JOB_REASONS,
    JOB_SCHEMA,
    JOB_STATES,
    RECOVERY_STATES,
    REFUSAL_CODES,
    REQUEST_SCHEMA,
    STATUS_SCHEMA,
    MediaRuntimeSetupError,
    MediaRuntimeSetupService,
    _Job,
    dispatch_setup_request,
)
from comfyui_h3_context.adapters.executable_admission import pin_exact_executable
from comfyui_h3_context.adapters.media_runtime_discovery_worker import (
    DiscoveryRequest,
    DiscoveryResponse,
    admit_request,
)
from comfyui_h3_context.adapters.media_runtime_installer import (
    SOURCE_URL,
    InstallerError,
    ManagedRuntimeInstaller,
    ManagedRuntimeManifest,
    RetainedMember,
    managed_runtime_manifest,
    parked_runtime_trees,
    reclaim_parked_runtimes,
)
from comfyui_h3_context.adapters.media_runtime_manager import FEATURES
from comfyui_h3_context.adapters.media_runtime_resolution import (
    MANAGED_PROFILE_COMPONENT,
    HostRootError,
    MediaRuntimeConfig,
    MediaRuntimeConfigError,
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    MediaRuntimeSelection,
    ResolutionReason,
    ResolutionState,
    RuntimeInputs,
    SourceKind,
    private_layout,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WIRE_FIXTURE = REPO_ROOT / "frontend/tests/fixtures/media_runtime_wire_v3.json"
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows managed runtime installer")

TOP = "pkg-full_build/"
FFMPEG = b"MZ hermetic ffmpeg " * 4096
FFPROBE = b"MZ hermetic ffprobe " * 4096
LICENSE = b"GNU GENERAL PUBLIC LICENSE fixture\n" * 64
README = b"source notice fixture\n" * 64
JOB = "ab" * 16
OTHER_JOB = "cd" * 16
THIRD_JOB = "ef" * 16
PLENTY = SimpleNamespace(free=10**12)


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_archive() -> bytes:
    entries: list[tuple[str, bytes | None]] = [
        (TOP, None),
        (f"{TOP}bin/", None),
        (f"{TOP}bin/ffmpeg.exe", FFMPEG),
        (f"{TOP}bin/ffprobe.exe", FFPROBE),
        (f"{TOP}LICENSE", LICENSE),
        (f"{TOP}README.txt", README),
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if payload is None:
                archive.writestr(zipfile.ZipInfo(name), b"")
            else:
                archive.writestr(name, payload)
    return buffer.getvalue()


def manifest_for(archive: bytes) -> ManagedRuntimeManifest:
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        infos = opened.infolist()
    return ManagedRuntimeManifest(
        profile_component=MANAGED_PROFILE_COMPONENT,
        source_url=SOURCE_URL,
        release_page_url="https://github.com/GyanD/codexffmpeg/releases/tag/fixture",
        source_label="fixture",
        license_name="GPL-3.0-or-later",
        archive_bytes=len(archive),
        archive_sha256=sha(archive),
        member_count=len(infos),
        expanded_bytes=sum(info.file_size for info in infos),
        top_directory=TOP,
        retained=(
            RetainedMember("bin/ffmpeg.exe", "bin/ffmpeg.exe", len(FFMPEG), sha(FFMPEG), True),
            RetainedMember("bin/ffprobe.exe", "bin/ffprobe.exe", len(FFPROBE), sha(FFPROBE), True),
            RetainedMember("LICENSE", "LICENSE.txt", len(LICENSE), sha(LICENSE), False),
            RetainedMember("README.txt", "README.txt", len(README), sha(README), False),
        ),
    )


ARCHIVE = build_archive()
MANIFEST = manifest_for(ARCHIVE)


class BytesDownloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download(
        self,
        url: str,
        *,
        expected_bytes: int,
        sink: Callable[[bytes], None],
        cancelled: threading.Event,
        progress: Callable[[int], None],
    ) -> None:
        sink(self.payload)
        progress(len(self.payload))

    def abort(self) -> None:
        return None


def run_installer(root: Path, token: str) -> None:
    ManagedRuntimeInstaller(
        private_layout(root),
        manifest=MANIFEST,
        downloader=BytesDownloader(ARCHIVE),
        job_token=token,
        cancelled=threading.Event(),
        progress=lambda *_args: None,
        disk_usage=lambda _path: PLENTY,
    ).run()


def write_tree(directory: Path, *, ffmpeg: bytes = FFMPEG) -> None:
    (directory / "bin").mkdir(parents=True)
    (directory / "bin" / "ffmpeg.exe").write_bytes(ffmpeg)
    (directory / "bin" / "ffprobe.exe").write_bytes(FFPROBE)
    (directory / "LICENSE.txt").write_bytes(LICENSE)


def park(root: Path, token: str = JOB) -> Path:
    """A retired tree as a publication whose restore failed leaves it."""

    retired = private_layout(root).media_runtime / "staging" / token / "retired"
    write_tree(retired, ffmpeg=b"stale")
    return retired


def publish(root: Path, *, ffmpeg: bytes = FFMPEG) -> Path:
    target = private_layout(root).runtime_root / MANAGED_PROFILE_COMPONENT
    write_tree(target, ffmpeg=ffmpeg)
    return target


def files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------------------------
# Service doubles


LOCATED = MediaRuntimeResolution(
    ResolutionState.LOCATED,
    ResolutionReason.PAIR_ADMITTED,
    SourceKind.MANAGED,
    ffmpeg_path=Path("C:/private/bin/ffmpeg.exe"),
    ffprobe_path=Path("C:/private/bin/ffprobe.exe"),
    scratch_root=Path("C:/private/scratch"),
    ffmpeg_identity=(1, 2, 3, 4),
    ffprobe_identity=(1, 2, 3, 5),
)
MISSING = MediaRuntimeResolution(
    ResolutionState.UNAVAILABLE, ResolutionReason.SUPPORTED_PAIR_MISSING
)
OVERRIDE = MediaRuntimeResolution(
    ResolutionState.LOCATED,
    ResolutionReason.PAIR_ADMITTED,
    SourceKind.EXPLICIT_OVERRIDE,
    ffmpeg_path=Path("C:/override/ffmpeg.exe"),
    ffprobe_path=Path("C:/override/ffprobe.exe"),
    scratch_root=Path("C:/private/scratch"),
    ffmpeg_identity=(1, 2, 3, 6),
    ffprobe_identity=(1, 2, 3, 7),
)


class FakeResolver:
    def __init__(self, result: MediaRuntimeResolution) -> None:
        self.result = result
        self.config: MediaRuntimeConfig | None = None
        self.layout = private_layout(Path("C:/private"))

    def resolve(self, *, rescan: bool = False) -> MediaRuntimeResolution:
        return self.result

    def invalidate(self) -> None:
        return None

    def read_config(self) -> MediaRuntimeConfig | None:
        return self.config

    def private_layout(self) -> Any:
        return self.layout


class Reclaims:
    def __init__(self, error: InstallerError | None = None) -> None:
        self.calls: list[tuple[object, object]] = []
        self.error = error

    def __call__(self, layout: object, manifest: object) -> int:
        self.calls.append((layout, manifest))
        if self.error is not None:
            raise self.error
        return 1


READY_FEATURES = {feature: {"state": "ready", "reason": None} for feature in FEATURES}


def service(
    resolver: FakeResolver,
    *,
    parked: tuple[Path, ...] = (),
    reclaimer: Reclaims | None = None,
    manager: object | None = None,
    starter: Callable[[Callable[[], None]], None] | None = None,
) -> MediaRuntimeSetupService:
    return MediaRuntimeSetupService(
        resolver=resolver,  # type: ignore[arg-type]
        manager=cast(Any, manager),
        downloader_factory=lambda: cast(Any, BytesDownloader(b"")),
        installer_factory=lambda _layout, **_kwargs: SimpleNamespace(run=lambda: None),
        manifest_provider=managed_runtime_manifest,
        thread_starter=starter or (lambda target: target()),
        job_id_factory=lambda: JOB,
        parked_scanner=lambda _layout, _manifest: parked,
        reclaimer=reclaimer or Reclaims(),
    )


def ready_manager() -> SimpleNamespace:
    return SimpleNamespace(
        current=lambda: object(),
        ensure=lambda: None,
        readiness=lambda: READY_FEATURES,
    )


# ---------------------------------------------------------------------------------------------
# Status v3


def test_status_v3_reports_no_recovery_without_a_parked_tree() -> None:
    status = service(FakeResolver(LOCATED), manager=ready_manager()).status()

    assert status["schema"] == STATUS_SCHEMA == "h3.context.media_runtime_status.v3"
    assert list(status) == [
        "schema",
        "resolution",
        "config",
        "setup",
        "install",
        "features",
        "recovery",
        "actions",
    ]
    assert status["recovery"] is None
    assert "reclaim_parked_runtime" not in cast(list[str], status["actions"])


def test_a_parked_tree_beside_a_located_managed_pair_is_reclaimable() -> None:
    status = service(
        FakeResolver(LOCATED), parked=(Path("C:/private/staging/job"),), manager=ready_manager()
    ).status()

    assert status["recovery"] == {"state": "parked_runtime", "reclaimable": True}
    assert status["actions"] == ["rescan", "use_local_directory", "reclaim_parked_runtime"]
    assert "staging" not in json.dumps(status)


@pytest.mark.parametrize(
    "resolution",
    [MISSING, OVERRIDE],
    ids=["no_published_pair", "override_not_managed"],
)
def test_a_parked_tree_without_a_managed_replacement_is_visible_but_not_reclaimable(
    resolution: MediaRuntimeResolution,
) -> None:
    status = service(FakeResolver(resolution), parked=(Path("C:/p"),)).status()

    assert status["recovery"] == {"state": "parked_runtime", "reclaimable": False}
    assert "reclaim_parked_runtime" not in cast(list[str], status["actions"])


def test_a_running_job_withholds_reclaim() -> None:
    started: list[Callable[[], None]] = []
    setup = service(FakeResolver(LOCATED), parked=(Path("C:/p"),), starter=started.append)
    setup.start_install()

    status = setup.status()

    assert status["recovery"] == {"state": "parked_runtime", "reclaimable": False}
    assert status["actions"] == ["cancel_setup"]


def test_a_private_root_that_cannot_be_read_reports_no_recovery() -> None:
    resolver = FakeResolver(MISSING)

    def unavailable() -> Any:
        raise MediaRuntimeConfigError("private_root_invalid")

    resolver.private_layout = unavailable  # type: ignore[method-assign]
    scans: list[object] = []

    def scan(layout: object, _manifest: object) -> tuple[Path, ...]:
        scans.append(layout)
        return (Path("C:/p"),)

    setup = MediaRuntimeSetupService(
        resolver=resolver,  # type: ignore[arg-type]
        manifest_provider=managed_runtime_manifest,
        parked_scanner=scan,
    )

    assert setup.status()["recovery"] is None
    assert scans == []


# ---------------------------------------------------------------------------------------------
# Reclaim action


def request(action: str, **fields: object) -> dict[str, object]:
    return {"schema": REQUEST_SCHEMA, "action": action, **fields}


def test_reclaim_is_one_closed_action_that_returns_the_refreshed_status() -> None:
    reclaims = Reclaims()
    setup = service(FakeResolver(LOCATED), parked=(Path("C:/p"),), reclaimer=reclaims)

    status_code, wire = dispatch_setup_request(setup, request("reclaim_parked_runtime"))

    assert status_code == 200
    assert wire["schema"] == STATUS_SCHEMA
    assert len(reclaims.calls) == 1
    with pytest.raises(MediaRuntimeSetupError, match="^invalid_request$"):
        dispatch_setup_request(setup, request("reclaim_parked_runtime", job_id=JOB))
    assert len(reclaims.calls) == 1


@pytest.mark.parametrize(
    ("code", "reported", "status"),
    [
        ("reclaim_unsafe", "reclaim_unsafe", 409),
        ("setup_busy", "setup_busy", 409),
        ("private_root_invalid", "private_root_invalid", 503),
        ("unsupported_platform", "unsupported_platform", 503),
        ("write_failed", "internal_failure", 500),
    ],
)
def test_reclaim_refusals_are_closed_and_release_the_action_slot(
    code: str, reported: str, status: int
) -> None:
    setup = service(FakeResolver(LOCATED), reclaimer=Reclaims(InstallerError(code)))

    with pytest.raises(MediaRuntimeSetupError) as refused:
        setup.reclaim_parked()

    assert (refused.value.code, refused.value.status) == (reported, status)
    assert reported in REFUSAL_CODES
    # The slot is free again: a following action is not refused as busy.
    setup.rescan()


def test_reclaim_is_refused_while_an_install_job_holds_the_action_slot() -> None:
    reclaims = Reclaims()
    started: list[Callable[[], None]] = []
    setup = service(FakeResolver(MISSING), reclaimer=reclaims, starter=started.append)
    setup.start_install()

    with pytest.raises(MediaRuntimeSetupError, match="^setup_busy$"):
        setup.reclaim_parked()

    assert reclaims.calls == []


def test_an_unknown_job_reason_is_reported_as_an_internal_failure() -> None:
    started: list[Callable[[], None]] = []
    setup = service(FakeResolver(MISSING), starter=started.append)
    job_id = cast(str, setup.start_install()["job_id"])

    setup._finish(cast(_Job, setup._current), "failed", "not_a_closed_reason")

    assert setup.job(job_id)["reason"] == "internal_failure"


# ---------------------------------------------------------------------------------------------
# Parked-tree scan and reclaim on the real filesystem


@windows_only
def test_the_scan_reports_only_plain_parked_trees_with_named_members(tmp_path: Path) -> None:
    import _winapi

    root = tmp_path / "private"
    layout = private_layout(root)
    assert parked_runtime_trees(layout, MANIFEST) == ()

    park(root, JOB)
    staging = layout.media_runtime / "staging"
    unknown_only = staging / OTHER_JOB / "retired"
    unknown_only.mkdir(parents=True)
    (unknown_only / "notes.dat").write_bytes(b"not named")
    (staging / "not-a-job" / "retired" / "bin").mkdir(parents=True)
    (staging / "not-a-job" / "retired" / "bin" / "ffmpeg.exe").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    write_tree(elsewhere)
    (staging / THIRD_JOB).mkdir()
    # Windows-only fixture APIs are absent from POSIX stubs, but must stay real junctions.
    getattr(_winapi, "CreateJunction")(str(elsewhere), str(staging / THIRD_JOB / "retired"))  # noqa: B009

    assert parked_runtime_trees(layout, MANIFEST) == (staging / JOB,)


@windows_only
def test_a_junctioned_member_directory_is_not_a_parked_member(tmp_path: Path) -> None:
    import _winapi

    root = tmp_path / "private"
    layout = private_layout(root)
    retired = layout.media_runtime / "staging" / JOB / "retired"
    retired.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    write_tree(elsewhere)
    getattr(_winapi, "CreateJunction")(str(elsewhere / "bin"), str(retired / "bin"))  # noqa: B009

    assert parked_runtime_trees(layout, MANIFEST) == ()
    assert reclaim_parked_runtimes(layout, MANIFEST) == 0
    assert files(elsewhere) == ["LICENSE.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"]


@windows_only
def test_reclaim_removes_only_named_members_after_verifying_the_published_pair(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    layout = private_layout(root)
    retired = park(root)
    (retired / "user-note.txt").write_bytes(b"not named")
    target = publish(root)

    assert reclaim_parked_runtimes(layout, MANIFEST) == 1

    assert files(retired) == ["user-note.txt"]
    assert files(target) == ["LICENSE.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"]
    assert parked_runtime_trees(layout, MANIFEST) == ()


@windows_only
@pytest.mark.parametrize("published", ["absent", "wrong_bytes"])
def test_reclaim_without_a_verified_replacement_deletes_nothing(
    tmp_path: Path, published: str
) -> None:
    root = tmp_path / "private"
    layout = private_layout(root)
    retired = park(root)
    if published == "wrong_bytes":
        publish(root, ffmpeg=b"MZ not the pinned build")

    with pytest.raises(InstallerError, match="^reclaim_unsafe$"):
        reclaim_parked_runtimes(layout, MANIFEST)

    assert files(retired) == ["LICENSE.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"]


@windows_only
def test_reclaim_waits_for_nobody_and_deletes_nothing_while_another_holds_the_lock(
    tmp_path: Path,
) -> None:
    import msvcrt

    root = tmp_path / "private"
    layout = private_layout(root)
    retired = park(root)
    publish(root)
    holder = os.open(layout.media_runtime / "staging" / "install.lock", os.O_RDWR | os.O_CREAT)
    locking = cast(Callable[[int, int, int], None], getattr(msvcrt, "locking"))  # noqa: B009
    locking(holder, int(getattr(msvcrt, "LK_NBLCK")), 1)  # noqa: B009
    try:
        with pytest.raises(InstallerError, match="^setup_busy$"):
            reclaim_parked_runtimes(layout, MANIFEST)
    finally:
        locking(holder, int(getattr(msvcrt, "LK_UNLCK")), 1)  # noqa: B009
        os.close(holder)

    assert files(retired) == ["LICENSE.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"]


@windows_only
def test_reclaim_takes_the_install_lock_before_the_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    layout = private_layout(root)
    park(root)
    publish(root)
    events: list[str] = []
    real_acquire = installer_module._acquire_install_lock
    real_release = installer_module._release_install_lock

    def acquire(staging: Path) -> int:
        events.append("lock")
        return real_acquire(staging)

    def release(descriptor: int) -> None:
        events.append("unlock")
        real_release(descriptor)

    def pin(path: Path, digest: str) -> Any:
        events.append(f"pin:{path.name}")
        return pin_exact_executable(path, digest)

    monkeypatch.setattr(installer_module, "_acquire_install_lock", acquire)
    monkeypatch.setattr(installer_module, "_release_install_lock", release)

    assert reclaim_parked_runtimes(layout, MANIFEST, pin=pin) == 1
    assert events == ["lock", "pin:ffmpeg.exe", "pin:ffprobe.exe", "unlock"]


class FakeRoots:
    def __init__(self, root: Path) -> None:
        self.root = root

    def private_root(self) -> Path:
        return self.root

    def served_roots(self) -> tuple[Path, ...]:
        raise HostRootError(ResolutionReason.PRIVATE_ROOT_INVALID)


class InProcessWorker:
    def run(self, request: DiscoveryRequest, *, timeout_seconds: float) -> DiscoveryResponse:
        return admit_request(request, drive_type=lambda _path: 3)

    def cancel(self) -> None:
        return None


def no_path_inputs() -> RuntimeInputs:
    return RuntimeInputs(
        platform="win32",
        machine="AMD64",
        pointer_bits=64,
        python_prefix="C:\\h3-absent-prefix",
        path=None,
        legacy=(),
    )


@windows_only
def test_a_failed_restore_is_parked_through_installs_and_reclaimed_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "user" / "__h3_context"
    layout = private_layout(root)
    publish(root, ffmpeg=b"stale")
    real_rename = os.rename

    def failing(source: object, destination: object) -> None:
        if Path(str(source)).name in {"tree", "retired"}:
            raise OSError(22, "failure", None, 87)
        real_rename(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", failing)
    with pytest.raises(InstallerError, match="^publication_failed$"):
        run_installer(root, JOB)
    monkeypatch.setattr(os, "rename", real_rename)
    retired = layout.media_runtime / "staging" / JOB / "retired"
    assert (retired / "bin" / "ffmpeg.exe").read_bytes() == b"stale"

    resolver = MediaRuntimeResolver(
        host_roots=FakeRoots(root),
        worker=InProcessWorker(),
        package_parent=tmp_path / "package",
        inputs_provider=no_path_inputs,
        ffmpeg_sha256=sha(FFMPEG),
        ffprobe_sha256=sha(FFPROBE),
    )
    setup = MediaRuntimeSetupService(
        resolver=resolver,
        manifest_provider=lambda: MANIFEST,
        thread_starter=lambda target: target(),
    )
    # No published pair: visible, not reclaimable, and a status read deletes nothing.
    before = setup.status()
    assert before["recovery"] == {"state": "parked_runtime", "reclaimable": False}
    with pytest.raises(MediaRuntimeSetupError, match="^reclaim_unsafe$"):
        setup.reclaim_parked()
    assert (retired / "bin" / "ffmpeg.exe").read_bytes() == b"stale"

    run_installer(root, OTHER_JOB)
    resolver.invalidate()
    resolver.resolve(rescan=True)
    installed = setup.status()
    assert installed["recovery"] == {"state": "parked_runtime", "reclaimable": True}
    assert (retired / "bin" / "ffmpeg.exe").read_bytes() == b"stale"

    reclaimed = setup.reclaim_parked()

    assert reclaimed["recovery"] is None
    assert "reclaim_parked_runtime" not in cast(list[str], reclaimed["actions"])
    assert not (retired / "bin" / "ffmpeg.exe").exists()
    published = layout.runtime_root / MANAGED_PROFILE_COMPONENT / "bin" / "ffmpeg.exe"
    assert published.read_bytes() == FFMPEG


# ---------------------------------------------------------------------------------------------
# Closed vocabularies and the client/server parity fixture


def _string_constants(node: ast.AST) -> set[str]:
    docstrings = {
        id(child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)
    }
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and id(child) not in docstrings
    }


def _function(tree: ast.Module, class_name: str | None, name: str) -> ast.AST:
    scope: list[ast.stmt] = tree.body
    if class_name is not None:
        owner = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        scope = owner.body
    return next(node for node in scope if isinstance(node, ast.FunctionDef) and node.name == name)


def test_every_feature_reason_the_manager_can_emit_is_in_the_closed_vocabulary() -> None:
    source = (REPO_ROOT / "comfyui_h3_context/adapters/media_runtime_manager.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    emitted = _string_constants(_function(tree, "MediaRuntimeManager", "_unavailable_reason"))
    emitted |= _string_constants(_function(tree, "MediaRuntimeManager", "readiness"))
    failures = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "failure"
            for target in node.targets
        )
    }
    assert failures == {"activation_failed"}
    states_and_keys = set(FEATURE_STATES) | {"state", "reason", "render"}
    assert emitted - states_and_keys <= FEATURE_REASONS
    assert failures <= FEATURE_REASONS


def test_every_job_phase_reported_by_the_installer_and_service_is_closed() -> None:
    phases: set[str] = set()
    for relative in (
        "comfyui_h3_context/adapters/media_runtime_installer.py",
        "comfyui_h3_context/adapters/comfyui_media_runtime_setup.py",
    ):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_progress"
            ):
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        phases.add(argument.value)
    phases |= {_Job(job_id=JOB, cancelled=threading.Event()).phase, "done"}
    assert phases == set(JOB_PHASES)


def test_every_precheck_and_finish_reason_literal_is_closed() -> None:
    tree = ast.parse(
        (REPO_ROOT / "comfyui_h3_context/adapters/comfyui_media_runtime_setup.py").read_text(
            encoding="utf-8"
        )
    )
    literals = _string_constants(_function(tree, "MediaRuntimeSetupService", "_precheck"))
    literals |= _string_constants(_function(tree, "MediaRuntimeSetupService", "_run"))
    literals -= set(JOB_STATES) | {"cancelled", "activating"}
    assert literals <= JOB_REASONS


def _sample_manifest() -> ManagedRuntimeManifest:
    return managed_runtime_manifest()


def _status(
    resolution: MediaRuntimeResolution,
    *,
    parked: bool = False,
    manager: object | None = None,
    job: tuple[str, str, str | None, int, int] | None = None,
    config: MediaRuntimeConfig | None = None,
) -> dict[str, object]:
    resolver = FakeResolver(resolution)
    resolver.config = config
    setup = service(resolver, parked=(Path("C:/p"),) if parked else (), manager=manager)
    if job is not None:
        state, phase, reason, completed, total = job
        record = _Job(
            job_id=JOB,
            cancelled=threading.Event(),
            state=state,
            phase=phase,
            reason=reason,
            completed_bytes=completed,
            total_bytes=total,
        )
        if state == "running":
            setup._current = record
        else:
            setup._last = record
    return setup.status()


def wire_fixture() -> dict[str, object]:
    """The canonical client/server agreement; regenerate the file from this on a wire change."""

    return {
        "schema": "h3.context.media_runtime_wire_fixture.v1",
        "status_schema": STATUS_SCHEMA,
        "job_schema": JOB_SCHEMA,
        "request_schema": REQUEST_SCHEMA,
        "vocabulary": {
            "actions": list(ACTIONS),
            "config_selections": [selection.value for selection in MediaRuntimeSelection],
            "feature_reasons": sorted(FEATURE_REASONS),
            "feature_states": list(FEATURE_STATES),
            "features": list(FEATURES),
            "job_phases": list(JOB_PHASES),
            "job_reasons": sorted(JOB_REASONS),
            "job_states": list(JOB_STATES),
            "recovery_states": list(RECOVERY_STATES),
            "refusal_codes": sorted(REFUSAL_CODES),
            "resolution_reasons": [reason.value for reason in ResolutionReason],
            "resolution_states": [state.value for state in ResolutionState],
            "source_kinds": [kind.value for kind in SourceKind],
        },
        "samples": {
            "status_setup_required": _status(MISSING),
            "status_installing": _status(
                MISSING, job=("running", "downloading", None, 1_048_576, 246_558_061)
            ),
            "status_install_failed": _status(
                MISSING, job=("failed", "done", "digest_mismatch", 0, 0)
            ),
            "status_ready_recovery": _status(LOCATED, parked=True, manager=ready_manager()),
            "status_local_selection": _status(
                LOCATED,
                manager=ready_manager(),
                config=MediaRuntimeConfig(4, MediaRuntimeSelection.LOCAL, "C:\\tools"),
            ),
            "status_override": _status(OVERRIDE, manager=ready_manager()),
            "job_running": _Job(
                job_id=JOB,
                cancelled=threading.Event(),
                phase="extracting",
                completed_bytes=10,
                total_bytes=20,
            ).to_wire(),
            "job_succeeded": _Job(
                job_id=JOB,
                cancelled=threading.Event(),
                state="succeeded",
                phase="done",
                reason="installed",
            ).to_wire(),
        },
    }


def test_the_wire_fixture_the_browser_decodes_is_the_servers_own_output() -> None:
    expected = json.loads(json.dumps(wire_fixture()))
    assert json.loads(WIRE_FIXTURE.read_text(encoding="utf-8")) == expected
    assert "C:" not in json.dumps(expected["samples"])


if __name__ == "__main__":  # pragma: no cover - fixture regeneration helper
    WIRE_FIXTURE.write_text(json.dumps(wire_fixture(), indent=2) + "\n", encoding="utf-8")
