"""Owned media runtime status/setup routes and the one setup job they drive.

The user-facing contract is small: read the current media tool resolution, install the one fixed
supported build when it is missing, rescan, choose one advanced local tool directory, return to
automatic selection, and cancel a running install. The server chooses the source, the destination
and every command; a client supplies at most an action name, a job identity, one directory and a
config revision.

Nothing here runs at import or registration. The service is a composition-root component built on
first request, and the install job runs on its own thread so no route handler waits on a download.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .comfyui_route_seam import (
    OriginRule,
    RoutePolicy,
    RouteResult,
    already_owned,
    host_web_and_routes,
    register_owned_route,
    route_method_available,
)
from .composition_root import MEDIA_RUNTIME_SETUP, component
from .media_runtime_discovery_worker import MAX_LOCATOR_CHARS
from .media_runtime_download import DownloadPort, HttpsArchiveDownloader
from .media_runtime_installer import (
    InstallerError,
    ManagedRuntimeInstaller,
    ManagedRuntimeManifest,
    managed_runtime_manifest,
    parked_runtime_trees,
    reclaim_parked_runtimes,
)
from .media_runtime_manager import FEATURES, MediaRuntimeBusy
from .media_runtime_resolution import (
    MediaRuntimeConfigError,
    MediaRuntimePrivateLayout,
    MediaRuntimeResolution,
    MediaRuntimeResolver,
    MediaRuntimeSelection,
    ResolutionReason,
    ResolutionState,
    SourceKind,
)

if TYPE_CHECKING:
    from .media_runtime_manager import MediaRuntimeManager

STATUS_SCHEMA = "h3.context.media_runtime_status.v3"
JOB_SCHEMA = "h3.context.media_runtime_setup_job.v1"
REQUEST_SCHEMA = "h3.context.media_runtime_setup_request.v1"
STATUS_ROUTE = "/h3-context/v1/media-runtime"
SETUP_ROUTE = "/h3-context/v1/media-runtime/setup"
JOB_ROUTE = "/h3-context/v1/media-runtime/setup/{job_id}"
MAX_SETUP_REQUEST_BYTES = 8 * 1024
MAX_CONFIG_REVISION = 2**53 - 1
ACTIONS = (
    "install_supported",
    "rescan",
    "use_local_directory",
    "restore_auto",
    "cancel_setup",
    "reclaim_parked_runtime",
)
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_media_runtime_route_owner__"
_ROUTE_REGISTERED = False
_REQUEST_KEYS: Mapping[str, frozenset[str]] = {
    "install_supported": frozenset({"schema", "action"}),
    "rescan": frozenset({"schema", "action"}),
    "cancel_setup": frozenset({"schema", "action", "job_id"}),
    "use_local_directory": frozenset({"schema", "action", "directory", "expected_revision"}),
    "restore_auto": frozenset({"schema", "action", "expected_revision"}),
    "reclaim_parked_runtime": frozenset({"schema", "action"}),
}

# Closed wire vocabularies. The browser decoder refuses any value outside them, so every value the
# service or the manager can emit belongs here, and the M25-33 parity fixture pins both sides.
FEATURE_STATES = ("ready", "setup_required", "unavailable")
FEATURE_REASONS = frozenset(
    {"activating", "discovering", "activation_failed", "invalid_config"}
    | {"render_qualification_unavailable"}
    | {reason.value for reason in ResolutionReason}
)
JOB_STATES = ("running", "succeeded", "failed", "cancelled")
JOB_PHASES = (
    "checking",
    "downloading",
    "extracting",
    "verifying",
    "publishing",
    "activating",
    "done",
)
JOB_REASONS = frozenset(
    {
        "cancelled",
        "already_available",
        "advanced_override_active",
        "config_invalid",
        "local_selection_active",
        "installed",
        "verification_failed",
        "internal_failure",
        MediaRuntimeBusy.code,
    }
    | InstallerError.CODES
    | MediaRuntimeConfigError.CODES
    | {reason.value for reason in ResolutionReason}
)
RECOVERY_STATES = ("parked_runtime",)
_RECLAIM_ERROR_STATUS: Mapping[str, int] = {
    "reclaim_unsafe": 409,
    "setup_busy": 409,
    "private_root_invalid": 503,
    "unsupported_platform": 503,
}
_CONFIG_ERROR_STATUS: Mapping[str, int] = {
    "config_conflict": 409,
    "config_corrupt": 409,
    "config_unsupported": 409,
    "invalid_request": 400,
    "selection_invalid_path": 422,
    "selection_unsupported_pair": 422,
    "private_root_invalid": 503,
    "unsupported_host": 503,
    "unsupported_platform": 503,
    "discovery_timeout": 503,
    "discovery_limit": 503,
    "worker_failure": 503,
    "config_write_failed": 500,
    "media_runtime_busy": 409,
}
REFUSAL_CODES = (
    frozenset(_CONFIG_ERROR_STATUS)
    | frozenset(_RECLAIM_ERROR_STATUS)
    | {"setup_busy", "setup_job_not_found", "internal_failure"}
    | {"media_type_rejected", "origin_rejected", "request_too_large"}
)


class MediaRuntimeSetupError(RuntimeError):
    """A closed refusal with the HTTP status it answers with."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


# --------------------------------------------------------------------------------------------
# Job


@dataclass(slots=True)
class _Job:
    job_id: str
    cancelled: threading.Event
    state: str = "running"
    phase: str = "checking"
    reason: str | None = None
    completed_bytes: int = 0
    total_bytes: int = 0
    downloader: DownloadPort | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema": JOB_SCHEMA,
            "job_id": self.job_id,
            "state": self.state,
            "phase": self.phase,
            "reason": self.reason,
            "progress": {
                "completed_bytes": self.completed_bytes,
                "total_bytes": self.total_bytes,
            },
        }


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="h3-media-runtime-setup", daemon=True).start()


def _new_job_id() -> str:
    return secrets.token_hex(16)


InstallerFactory = Callable[..., Any]
ParkedScanner = Callable[[MediaRuntimePrivateLayout, ManagedRuntimeManifest], tuple[Any, ...]]
Reclaimer = Callable[[MediaRuntimePrivateLayout, ManagedRuntimeManifest], int]


class MediaRuntimeSetupService:
    """One setup job per process, and serialized setup actions."""

    def __init__(
        self,
        *,
        resolver: MediaRuntimeResolver,
        manager: MediaRuntimeManager | None = None,
        downloader_factory: Callable[[], DownloadPort] = HttpsArchiveDownloader,
        installer_factory: InstallerFactory = ManagedRuntimeInstaller,
        manifest_provider: Callable[[], ManagedRuntimeManifest] = managed_runtime_manifest,
        thread_starter: Callable[[Callable[[], None]], None] = _start_daemon,
        job_id_factory: Callable[[], str] = _new_job_id,
        parked_scanner: ParkedScanner = parked_runtime_trees,
        reclaimer: Reclaimer = reclaim_parked_runtimes,
    ) -> None:
        self._resolver = resolver
        self._manager = manager
        self._downloader_factory = downloader_factory
        self._installer_factory = installer_factory
        self._manifest_provider = manifest_provider
        self._thread_starter = thread_starter
        self._job_id_factory = job_id_factory
        self._parked_scanner = parked_scanner
        self._reclaimer = reclaimer
        self._lock = threading.Lock()
        # IMPORTANT: one action slot shared by the install job and every config/rescan action.
        # The job holds it from start to finish, so a local-directory write cannot race the
        # publication rename, and a rescan cannot supersede the job's own activation resolve.
        self._action_slot = threading.Lock()
        self._current: _Job | None = None
        self._last: _Job | None = None

    def __repr__(self) -> str:
        return "<MediaRuntimeSetupService>"

    # -- reads -----------------------------------------------------------------------------

    def status(self) -> dict[str, object]:
        """Blocking: may run bounded local discovery and first-use activation, never a download."""

        resolution = self._resolver.resolve()
        if (
            self._manager is not None
            and resolution.state is ResolutionState.LOCATED
            and self._manager.current() is None
        ):
            self._manager.ensure()
        config = self._config_wire()
        parked = self._parked()
        with self._lock:
            job = self._current or self._last
            job_wire = None if job is None else job.to_wire()
            running = self._current is not None
        # The published managed pair is what makes a parked tree safe to remove. The resolver's
        # located managed binding is the cheap read here; the reclaim itself re-pins the pair
        # under the install lock before deleting anything.
        reclaimable = (
            parked
            and not running
            and resolution.state is ResolutionState.LOCATED
            and resolution.source_kind is SourceKind.MANAGED
        )
        return {
            "schema": STATUS_SCHEMA,
            "resolution": resolution.to_wire(),
            "config": config,
            "setup": job_wire,
            "install": self._install_wire(),
            "features": self._features(resolution),
            "recovery": (
                {"state": "parked_runtime", "reclaimable": reclaimable} if parked else None
            ),
            "actions": list(
                self._actions(resolution, config, running=running, reclaimable=reclaimable)
            ),
        }

    def _parked(self) -> bool:
        try:
            layout = self._resolver.private_layout()
        except MediaRuntimeConfigError:
            return False
        return bool(self._parked_scanner(layout, self._manifest_provider()))

    def _features(self, resolution: MediaRuntimeResolution) -> dict[str, dict[str, str | None]]:
        if self._manager is not None:
            return self._manager.readiness()
        state = "setup_required" if resolution.install_resolves else "unavailable"
        return {
            feature: {"state": state, "reason": resolution.reason.value} for feature in FEATURES
        }

    def job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            for job in (self._current, self._last):
                if job is not None and job.job_id == job_id:
                    return job.to_wire()
        raise MediaRuntimeSetupError("setup_job_not_found", 404)

    def _config_wire(self) -> dict[str, object] | None:
        try:
            config = self._resolver.read_config()
        except MediaRuntimeConfigError:
            return None
        if config is None:
            return {"revision": 0, "selection": MediaRuntimeSelection.AUTO.value}
        return {"revision": config.revision, "selection": config.selection.value}

    def _install_wire(self) -> dict[str, object]:
        manifest = self._manifest_provider()
        return {
            "profile": manifest.profile_component,
            "source_label": manifest.source_label,
            "release_page": manifest.release_page_url,
            "license": manifest.license_name,
            "approximate_bytes": manifest.archive_bytes,
        }

    @staticmethod
    def _actions(
        resolution: MediaRuntimeResolution,
        config: Mapping[str, object] | None,
        *,
        running: bool,
        reclaimable: bool = False,
    ) -> tuple[str, ...]:
        if running:
            return ("cancel_setup",)
        available = {"rescan"}
        if reclaimable:
            available.add("reclaim_parked_runtime")
        if resolution.source_kind is not SourceKind.EXPLICIT_OVERRIDE and config is not None:
            available.add("use_local_directory")
            if config["selection"] == MediaRuntimeSelection.LOCAL.value:
                available.add("restore_auto")
            elif resolution.install_resolves:
                available.add("install_supported")
        return tuple(action for action in ACTIONS if action in available)

    # -- install job -----------------------------------------------------------------------

    def start_install(self) -> dict[str, object]:
        with self._lock:
            if self._current is not None:
                # A duplicate request -- a second click, another tab -- shares the running job.
                return self._current.to_wire()
            if not self._action_slot.acquire(blocking=False):
                raise MediaRuntimeSetupError("setup_busy", 409)
            job = _Job(job_id=self._job_id_factory(), cancelled=threading.Event())
            self._current = job
            wire = job.to_wire()
        try:
            self._thread_starter(lambda: self._run(job))
        except BaseException:
            self._finish(job, "failed", "internal_failure")
            raise
        return wire

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            current = self._current
            if current is None or current.job_id != job_id:
                last = self._last
                if last is not None and last.job_id == job_id:
                    return last.to_wire()
                raise MediaRuntimeSetupError("setup_job_not_found", 404)
            current.cancelled.set()
            downloader = current.downloader
            wire = current.to_wire()
        if downloader is not None:
            downloader.abort()
        return wire

    def _progress(self, job: _Job, phase: str, completed: int, total: int) -> None:
        with self._lock:
            job.phase = phase
            job.completed_bytes = completed
            job.total_bytes = total

    def _finish(self, job: _Job, state: str, reason: str) -> None:
        with self._lock:
            if self._current is not job:
                return
            job.state = state
            # A reason outside the closed vocabulary would make every client refuse the job wire,
            # hiding a terminal job behind "status unavailable"; report it as an internal failure.
            job.reason = reason if reason in JOB_REASONS else "internal_failure"
            job.phase = "done"
            job.downloader = None
            self._current = None
            self._last = job
            self._action_slot.release()

    def _precheck(self, job: _Job) -> tuple[str, str] | None:
        resolution = self._resolver.resolve(rescan=True)
        if job.cancelled.is_set():
            return "cancelled", "cancelled"
        if resolution.state is ResolutionState.LOCATED:
            # Supported tools are already usable: no download, no write.
            return "succeeded", "already_available"
        if resolution.source_kind is SourceKind.EXPLICIT_OVERRIDE:
            # An administrator's explicit override controls the result; installing cannot repair
            # it and must not be offered as though it could.
            return "failed", "advanced_override_active"
        if resolution.source_kind is SourceKind.LOCAL_SELECTION:
            if resolution.reason in {
                ResolutionReason.CONFIG_CORRUPT,
                ResolutionReason.CONFIG_UNSUPPORTED,
            }:
                return "failed", "config_invalid"
            return "failed", "local_selection_active"
        if resolution.install_resolves:
            return None
        # IMPORTANT: only a search that examined the managed directory proceeds (see
        # `install_resolves`). A timeout or a limit reached before enumeration never proved the
        # managed directory is broken, and publication would retire a tree that may be valid.
        return "failed", resolution.reason.value

    def _run(self, job: _Job) -> None:
        try:
            verdict = self._precheck(job)
            if verdict is not None:
                self._finish(job, *verdict)
                return
            layout = self._resolver.private_layout()
            downloader = self._downloader_factory()
            with self._lock:
                job.downloader = downloader
            installer = self._installer_factory(
                layout,
                manifest=self._manifest_provider(),
                downloader=downloader,
                job_token=job.job_id,
                cancelled=job.cancelled,
                progress=lambda phase, done, total: self._progress(job, phase, done, total),
            )
            installer.run()
            self._progress(job, "activating", 0, 0)
            if self._activate_managed():
                self._finish(job, "succeeded", "installed")
            else:
                self._finish(job, "failed", "verification_failed")
        except InstallerError as exc:
            if exc.code == "cancelled":
                self._finish(job, "cancelled", "cancelled")
            else:
                self._finish(job, "failed", exc.code)
        except MediaRuntimeConfigError as exc:
            self._finish(job, "failed", exc.code)
        except MediaRuntimeBusy:
            self._finish(job, "failed", MediaRuntimeBusy.code)
        except Exception:  # noqa: BLE001 - a job thread must always reach a terminal state
            self._finish(job, "failed", "internal_failure")

    def _activate_managed(self) -> bool:
        """Apply a just-published install in this process; only a managed binding succeeds."""

        if self._manager is None:
            self._resolver.invalidate()
            resolution = self._resolver.resolve(rescan=True)
            return (
                resolution.state is ResolutionState.LOCATED
                and resolution.source_kind is SourceKind.MANAGED
            )
        binding = self._manager.refresh()
        return binding is not None and binding.source_kind is SourceKind.MANAGED

    # -- serialized actions ----------------------------------------------------------------

    def _exclusive(self, action: Callable[[], object]) -> None:
        if not self._action_slot.acquire(blocking=False):
            raise MediaRuntimeSetupError("setup_busy", 409)
        try:
            action()
        except MediaRuntimeConfigError as exc:
            raise MediaRuntimeSetupError(exc.code, _CONFIG_ERROR_STATUS.get(exc.code, 500)) from exc
        except MediaRuntimeBusy as exc:
            raise MediaRuntimeSetupError(exc.code, 409) from exc
        finally:
            self._action_slot.release()

    def _rescan(self) -> None:
        if self._manager is not None and self._manager.current() is None:
            # Newly found tools activate now; an active binding is never replaced by a rescan.
            self._manager.refresh()
            return
        self._resolver.resolve(rescan=True)

    def _config_transition(self, write: Callable[[], object]) -> None:
        if self._manager is None:
            write()
            return
        self._manager.refresh(transition=write)

    def rescan(self) -> dict[str, object]:
        self._exclusive(self._rescan)
        return self.status()

    def use_local_directory(self, directory: str, expected_revision: int) -> dict[str, object]:
        self._exclusive(
            lambda: self._config_transition(
                lambda: self._resolver.write_config(
                    expected_revision=expected_revision,
                    selection=MediaRuntimeSelection.LOCAL,
                    directory=directory,
                )
            )
        )
        return self.status()

    def restore_auto(self, expected_revision: int) -> dict[str, object]:
        self._exclusive(
            lambda: self._config_transition(
                lambda: self._resolver.write_config(
                    expected_revision=expected_revision, selection=MediaRuntimeSelection.AUTO
                )
            )
        )
        return self.status()

    def reclaim_parked(self) -> dict[str, object]:
        """Explicitly remove parked runtime trees; the reclaimer owns every safety check."""

        def reclaim() -> None:
            layout = self._resolver.private_layout()
            try:
                self._reclaimer(layout, self._manifest_provider())
            except InstallerError as exc:
                status = _RECLAIM_ERROR_STATUS.get(exc.code)
                if status is None:
                    raise MediaRuntimeSetupError("internal_failure", 500) from exc
                raise MediaRuntimeSetupError(exc.code, status) from exc

        self._exclusive(reclaim)
        return self.status()


# --------------------------------------------------------------------------------------------
# Wire decoding and dispatch


def _invalid() -> MediaRuntimeSetupError:
    return MediaRuntimeSetupError("invalid_request", 400)


def decode_setup_request(raw: bytes) -> Mapping[str, object]:
    """Strictly decode one bounded JSON object; size enforcement belongs to the route seam."""

    if type(raw) is not bytes:
        raise _invalid()

    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid()
            result[key] = value
        return result

    def refuse_constant(_value: str) -> object:
        raise _invalid()

    try:
        decoded = json.loads(
            raw.decode("utf-8"), object_pairs_hook=closed_object, parse_constant=refuse_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc
    if type(decoded) is not dict:
        raise _invalid()
    return decoded


def _revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CONFIG_REVISION:
        raise _invalid()
    return value


def dispatch_setup_request(
    service: MediaRuntimeSetupService, payload: Mapping[str, object]
) -> tuple[int, dict[str, object]]:
    action = payload.get("action")
    if payload.get("schema") != REQUEST_SCHEMA or type(action) is not str:
        raise _invalid()
    keys = _REQUEST_KEYS.get(action)
    if keys is None or set(payload) != keys:
        raise _invalid()
    if action == "install_supported":
        return 202, service.start_install()
    if action == "cancel_setup":
        job_id = payload["job_id"]
        if type(job_id) is not str or _JOB_ID.fullmatch(job_id) is None:
            raise _invalid()
        return 200, service.cancel(job_id)
    if action == "rescan":
        return 200, service.rescan()
    if action == "reclaim_parked_runtime":
        return 200, service.reclaim_parked()
    if action == "use_local_directory":
        directory = payload["directory"]
        if type(directory) is not str or not 0 < len(directory) <= MAX_LOCATOR_CHARS:
            raise _invalid()
        return 200, service.use_local_directory(directory, _revision(payload["expected_revision"]))
    return 200, service.restore_auto(_revision(payload["expected_revision"]))


# --------------------------------------------------------------------------------------------
# Routes


def build_media_runtime_setup(
    *, resolver: MediaRuntimeResolver, manager: MediaRuntimeManager
) -> MediaRuntimeSetupService:
    """Construct the process setup service. Called only by the composition root; performs no I/O."""

    return MediaRuntimeSetupService(resolver=resolver, manager=manager)


media_runtime_setup = component(MEDIA_RUNTIME_SETUP, MediaRuntimeSetupService)

_STATUS_POLICY = RoutePolicy(
    path=STATUS_ROUTE,
    owner=STATUS_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    # A status read can start local discovery work, so a cross-site navigation must not be able
    # to trigger it; same-origin and non-browser callers are admitted.
    origin=OriginRule.SAME_ORIGIN_GET,
    refusals=(MediaRuntimeSetupError,),
)
_SETUP_POLICY = RoutePolicy(
    path=SETUP_ROUTE,
    owner=REQUEST_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    max_bytes=MAX_SETUP_REQUEST_BYTES,
    refusals=(MediaRuntimeSetupError,),
)
_JOB_POLICY = RoutePolicy(
    path=JOB_ROUTE,
    owner=JOB_SCHEMA,
    owner_attribute=_ROUTE_OWNER_ATTRIBUTE,
    method="GET",
    origin=OriginRule.SAME_ORIGIN_GET,
    refusals=(MediaRuntimeSetupError,),
)


def _refusal_body(_status: int, reason: str) -> dict[str, str]:
    return {"error": reason}


def _refusal(error: BaseException) -> RouteResult:
    if type(error) is not MediaRuntimeSetupError:
        return RouteResult(500, {"error": "internal_failure"})
    return RouteResult(error.status, {"error": error.code})


def _job_prelude(request: object) -> object:
    match_info = getattr(request, "match_info", None)
    get_match = getattr(match_info, "get", None)
    job_id = get_match("job_id") if callable(get_match) else None
    if type(job_id) is not str or _JOB_ID.fullmatch(job_id) is None:
        return RouteResult(400, {"error": "invalid_request"})
    return job_id


async def _read_status(_payload: bytes, _context: object) -> RouteResult:
    service = media_runtime_setup()
    return RouteResult(200, await asyncio.to_thread(service.status))


async def _apply_setup(payload: bytes, _context: object) -> RouteResult:
    decoded = decode_setup_request(payload)
    status, wire = await asyncio.to_thread(dispatch_setup_request, media_runtime_setup(), decoded)
    return RouteResult(status, wire)


async def _read_job(_payload: bytes, context: object) -> RouteResult:
    if type(context) is not str:
        raise MediaRuntimeSetupError("invalid_request", 400)
    return RouteResult(200, media_runtime_setup().job(context))


def ensure_media_runtime_setup_route_registered() -> bool:
    """Register the status, setup and job routes all-or-nothing, without importing host modules."""

    global _ROUTE_REGISTERED
    found = host_web_and_routes()
    if found is None:
        _ROUTE_REGISTERED = False
        return False
    _, routes = found
    for policy in (_STATUS_POLICY, _SETUP_POLICY, _JOB_POLICY):
        if (
            not route_method_available(routes, policy)
            or already_owned(routes, policy, __name__) is False
        ):
            _ROUTE_REGISTERED = False
            return False
    status = register_owned_route(
        _STATUS_POLICY, __name__, _read_status, _refusal_body, refusal_mapper=_refusal
    )
    setup = register_owned_route(
        _SETUP_POLICY, __name__, _apply_setup, _refusal_body, refusal_mapper=_refusal
    )
    job = register_owned_route(
        _JOB_POLICY,
        __name__,
        _read_job,
        _refusal_body,
        refusal_mapper=_refusal,
        prelude=_job_prelude,
    )
    _ROUTE_REGISTERED = status and setup and job
    return _ROUTE_REGISTERED


__all__ = [
    "ACTIONS",
    "FEATURE_REASONS",
    "FEATURE_STATES",
    "JOB_PHASES",
    "JOB_REASONS",
    "JOB_ROUTE",
    "JOB_SCHEMA",
    "JOB_STATES",
    "MAX_SETUP_REQUEST_BYTES",
    "RECOVERY_STATES",
    "REFUSAL_CODES",
    "REQUEST_SCHEMA",
    "SETUP_ROUTE",
    "STATUS_ROUTE",
    "STATUS_SCHEMA",
    "MediaRuntimeSetupError",
    "MediaRuntimeSetupService",
    "build_media_runtime_setup",
    "decode_setup_request",
    "dispatch_setup_request",
    "ensure_media_runtime_setup_route_registered",
    "media_runtime_setup",
]
