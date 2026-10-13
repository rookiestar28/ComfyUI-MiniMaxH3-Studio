"""Owned, offline media-runtime discovery, local selection and explicit recovery routes.
No import or registration acquires or installs executable code.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
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
from .media_runtime_leftovers import (
    RecoveryError,
    abandoned_setup_jobs,
    parked_runtime_trees,
    reclaim_parked_runtimes,
)
from .media_runtime_manager import FEATURES, MediaRuntimeBusy
from .media_runtime_pin import MediaRuntimePin, media_runtime_pin
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

STATUS_SCHEMA = "h3.context.media_runtime_status.v4"
REQUEST_SCHEMA = "h3.context.media_runtime_setup_request.v2"
STATUS_ROUTE = "/h3-context/v1/media-runtime"
SETUP_ROUTE = "/h3-context/v1/media-runtime/setup"
MAX_SETUP_REQUEST_BYTES = 8 * 1024
MAX_CONFIG_REVISION = 2**53 - 1
ACTIONS = (
    "rescan",
    "use_local_directory",
    "restore_auto",
    "reclaim_parked_runtime",
)
_ROUTE_OWNER_ATTRIBUTE = "__h3_context_media_runtime_route_owner__"
_ROUTE_REGISTERED = False
_REQUEST_KEYS: Mapping[str, frozenset[str]] = {
    "rescan": frozenset({"schema", "action"}),
    "use_local_directory": frozenset({"schema", "action", "directory", "expected_revision"}),
    "restore_auto": frozenset({"schema", "action", "expected_revision"}),
    "reclaim_parked_runtime": frozenset({"schema", "action"}),
}

# Closed wire vocabularies. The browser decoder refuses any value outside them, so every value the
# service or the manager can emit belongs here, and the parity fixture pins both sides.
FEATURE_STATES = ("ready", "setup_required", "unavailable")
FEATURE_REASONS = frozenset(
    {"activating", "discovering", "activation_failed", "invalid_config"}
    | {"render_qualification_unavailable"}
    | {reason.value for reason in ResolutionReason}
)
RECOVERY_STATES = ("parked_runtime", "abandoned_setup")
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
    | {"setup_busy", "internal_failure"}
    | {"media_type_rejected", "origin_rejected", "request_too_large"}
)


class MediaRuntimeSetupError(RuntimeError):
    """A closed refusal with the HTTP status it answers with."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


# --------------------------------------------------------------------------------------------
# Recovery dependencies

ParkedScanner = Callable[[MediaRuntimePrivateLayout, MediaRuntimePin], tuple[Any, ...]]
Reclaimer = Callable[[MediaRuntimePrivateLayout, MediaRuntimePin], int]


class MediaRuntimeSetupService:
    """Serialized offline selection and explicit recovery actions."""

    def __init__(
        self,
        *,
        resolver: MediaRuntimeResolver,
        manager: MediaRuntimeManager | None = None,
        manifest_provider: Callable[[], MediaRuntimePin] = media_runtime_pin,
        parked_scanner: ParkedScanner = parked_runtime_trees,
        abandoned_scanner: ParkedScanner = abandoned_setup_jobs,
        reclaimer: Reclaimer = reclaim_parked_runtimes,
    ) -> None:
        self._resolver = resolver
        self._manager = manager
        self._manifest_provider = manifest_provider
        self._parked_scanner = parked_scanner
        self._abandoned_scanner = abandoned_scanner
        self._reclaimer = reclaimer
        # IMPORTANT: config transitions and cleanup share a nonblocking process action slot.
        # Cleanup must not race the binding/config transition that decides whether it is safe.
        self._action_slot = threading.Lock()

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
        parked, abandoned = self._leftovers()
        reclaimable = (
            abandoned
            and not parked
            or (
                parked
                and resolution.state is ResolutionState.LOCATED
                and resolution.source_kind is SourceKind.MANAGED
            )
        )
        return {
            "schema": STATUS_SCHEMA,
            "resolution": resolution.to_wire(),
            "config": config,
            "manual": self._manual_wire(),
            "features": self._features(resolution),
            "recovery": (
                {
                    "state": "parked_runtime" if parked else "abandoned_setup",
                    "reclaimable": reclaimable,
                }
                if parked or abandoned
                else None
            ),
            "actions": list(self._actions(resolution, config, reclaimable=reclaimable)),
        }

    def _leftovers(self) -> tuple[bool, bool]:
        try:
            layout = self._resolver.private_layout()
        except MediaRuntimeConfigError:
            return False, False
        manifest = self._manifest_provider()
        return bool(self._parked_scanner(layout, manifest)), bool(
            self._abandoned_scanner(layout, manifest)
        )

    def _features(self, resolution: MediaRuntimeResolution) -> dict[str, dict[str, str | None]]:
        if self._manager is not None:
            return self._manager.readiness()
        state = "setup_required" if resolution.install_resolves else "unavailable"
        return {
            feature: {"state": state, "reason": resolution.reason.value} for feature in FEATURES
        }

    def _config_wire(self) -> dict[str, object] | None:
        try:
            config = self._resolver.read_config()
        except MediaRuntimeConfigError:
            return None
        if config is None:
            return {"revision": 0, "selection": MediaRuntimeSelection.AUTO.value}
        return {"revision": config.revision, "selection": config.selection.value}

    def _manual_wire(self) -> dict[str, object]:
        manifest = self._manifest_provider()
        return {
            "archive_name": manifest.archive_filename,
            "archive_sha256": manifest.archive_sha256,
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
        reclaimable: bool = False,
    ) -> tuple[str, ...]:
        available = {"rescan"}
        if reclaimable:
            available.add("reclaim_parked_runtime")
        if resolution.source_kind is not SourceKind.EXPLICIT_OVERRIDE and config is not None:
            available.add("use_local_directory")
            if config["selection"] == MediaRuntimeSelection.LOCAL.value:
                available.add("restore_auto")
        return tuple(action for action in ACTIONS if action in available)

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
            except RecoveryError as exc:
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


def _refusal_body(_status: int, reason: str) -> dict[str, str]:
    return {"error": reason}


def _refusal(error: BaseException) -> RouteResult:
    if type(error) is not MediaRuntimeSetupError:
        return RouteResult(500, {"error": "internal_failure"})
    return RouteResult(error.status, {"error": error.code})


async def _read_status(_payload: bytes, _context: object) -> RouteResult:
    service = media_runtime_setup()
    return RouteResult(200, await asyncio.to_thread(service.status))


async def _apply_setup(payload: bytes, _context: object) -> RouteResult:
    decoded = decode_setup_request(payload)
    status, wire = await asyncio.to_thread(dispatch_setup_request, media_runtime_setup(), decoded)
    return RouteResult(status, wire)


def ensure_media_runtime_setup_route_registered() -> bool:
    """Register the status and setup routes all-or-nothing, without importing host modules."""

    global _ROUTE_REGISTERED
    found = host_web_and_routes()
    if found is None:
        _ROUTE_REGISTERED = False
        return False
    _, routes = found
    for policy in (_STATUS_POLICY, _SETUP_POLICY):
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
    _ROUTE_REGISTERED = status and setup
    return _ROUTE_REGISTERED


__all__ = [
    "ACTIONS",
    "FEATURE_REASONS",
    "FEATURE_STATES",
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
