"""Explicit retained-media commands bind current server, Production and media owners."""

from __future__ import annotations

import atexit
import re
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from fractions import Fraction
from typing import Any, cast

from ..core.av_reconstruction import qualified_av_limits
from ..core.durable_workspace_state import (
    MAX_OWNER_REVISION,
    DurableStateError,
    json_bytes,
    strict_json,
)
from ..core.retained_assets import CLOSED_RETENTION_MS, RetainedAssetError, require_asset_id
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    claim_transferred_authoring_source,
    execute_transferred_authoring_preview,
)
from .authoring_video_facts import AuthoringVideoFacts
from .av_reconstruction_media import AVMediaAdapterError, QualifiedAVMediaAdapter
from .comfyui_media_runtime import current_authorized_media_runtime, media_runtime_lease
from .comfyui_production_workspace import ProductionWorkbenchError, ProductionWorkspaceRegistry
from .recovery_owner import RecoveryOwner, RecoveryOwnerPort
from .retained_asset_source import check_retention_budget, stage_retention_source
from .retained_asset_store import RetainedAssetStore, RetainedInventory
from .retained_asset_use import RetainedAssetUse

RESPONSE_SCHEMA = "h3.context.retained_assets_response.v1"
STATUS_SCHEMA = "h3.context.retained_assets_status.v1"
MAX_ACTION_BYTES = 8192
_USE = re.compile(r"retained_[0-9a-f]{32}\Z")
_INTENTS = {
    "status": {"intent"},
    "list": {"intent"},
    "set_enabled": {"intent", "enabled", "expected_revision"},
    "restore": {"intent", "asset_id", "expected_revision"},
    "release": {"intent", "use_handle"},
    "clear": {"intent", "expected_revision"},
    "collect": {"intent", "expected_revision"},
    "retain": {
        "intent",
        "expected_revision",
        "workspace_handle",
        "workspace_id",
        "expected_workspace_revision",
        "expected_workspace_fingerprint",
        "segment_id",
        "output_handle",
    },
}


def _closed_action(payload: bytes) -> dict[str, object]:
    try:
        value = strict_json(payload, maximum_bytes=MAX_ACTION_BYTES, maximum_depth=4)
    except DurableStateError as error:
        raise RetainedAssetError(str(error)) from None
    if type(value) is not dict:
        raise RetainedAssetError("shape_invalid")
    return cast(dict[str, object], value)


def _use_handle(value: object) -> str:
    if type(value) is not str or _USE.fullmatch(value) is None:
        raise RetainedAssetError("lease_invalid")
    return value


def decode_retained_preview(payload: bytes) -> str:
    value = _closed_action(payload)
    if set(value) != {"use_handle"}:
        raise RetainedAssetError("shape_invalid")
    return _use_handle(value["use_handle"])


def decode_retained_action(payload: bytes) -> dict[str, object]:
    value = _closed_action(payload)
    intent = value.get("intent")
    if type(intent) is not str or intent not in _INTENTS or set(value) != _INTENTS[intent]:
        raise RetainedAssetError("intent_invalid")
    for field in ("expected_revision", "expected_workspace_revision"):
        if field in value and (
            type(value[field]) is not int or not 0 <= cast(int, value[field]) <= MAX_OWNER_REVISION
        ):
            raise RetainedAssetError("revision_invalid")
    if intent == "set_enabled" and type(value["enabled"]) is not bool:
        raise RetainedAssetError("intent_invalid")
    if intent == "restore":
        require_asset_id(value["asset_id"])
    if intent == "release":
        _use_handle(value["use_handle"])
    if intent == "retain":
        for field in (
            "workspace_handle",
            "workspace_id",
            "expected_workspace_fingerprint",
            "segment_id",
            "output_handle",
        ):
            if type(value[field]) is not str or not 1 <= len(cast(str, value[field])) <= 160:
                raise RetainedAssetError("intent_invalid")
    return value


def _media_refusal(error: Exception) -> RetainedAssetError:
    code = getattr(error, "code", "")
    if code in {"cancelled", "preview_cancelled"}:
        return RetainedAssetError("cancelled")
    if code in {"preview_deadline", "preview_timeout", "process_timeout"}:
        return RetainedAssetError("timed_out")
    if code in {"source_stale", "inspection_stale"}:
        return RetainedAssetError("source_stale")
    return RetainedAssetError("media_unqualified")


def _native_preview_fps(facts: AuthoringVideoFacts) -> int | None:
    step, base = facts.landmarks[0].duration_ticks, facts.source_time_base
    fps = Fraction(base.den, step * base.num)
    # IMPORTANT: preview trims native frame indices and checks the probed average FPS. Reducing
    # total duration to a different frames/FPS pair silently changes the clip and is refused.
    if fps.denominator != 1 or not 1 <= fps.numerator <= qualified_av_limits().max_input_fps:
        return None
    if any(
        row.pts != row.frame_index * step or row.duration_ticks != step for row in facts.landmarks
    ):
        return None
    return fps.numerator


class _Cancellation:
    def __init__(self, cancelled: Callable[[], bool]) -> None:
        self._cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self._cancelled()


class RetainedAssetService:
    def __init__(
        self,
        *,
        owner_port: RecoveryOwnerPort,
        production: Callable[[], ProductionWorkspaceRegistry],
        media_runtime: Callable[
            [], QualifiedAVMediaAdapter | None
        ] = current_authorized_media_runtime,
        lease: Callable[[], AbstractContextManager[None]] = media_runtime_lease,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.owner_port, self._production = owner_port, production
        self._media_runtime, self._lease, self._clock_ms = media_runtime, lease, clock_ms
        self._lock = threading.RLock()
        self._owner: RecoveryOwner | None = None
        self._store: RetainedAssetStore | None = None
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._store is not None:
                self._store.close()

    def qualify(self) -> RecoveryOwner:
        try:
            return self.owner_port.resolve()
        except DurableStateError as error:
            raise RetainedAssetError(str(error)) from None

    def _qualified_store(self, admitted_owner: RecoveryOwner | None) -> RetainedAssetStore:
        if self._closed:
            raise RetainedAssetError("service_closed")
        owner = self.qualify()
        if (admitted_owner is not None and owner != admitted_owner) or (
            self._owner is not None and owner != self._owner
        ):
            raise RetainedAssetError("owner_changed")
        if self._store is None:
            self._owner = owner
            self._store = RetainedAssetStore(
                owner.private_root, owner.owner_id, clock_ms=self._clock_ms
            )
        return self._store

    def _projection(self, inventory: RetainedInventory) -> dict[str, Any]:
        catalog, now = inventory.catalog, self._clock_ms()
        return {
            "schema": STATUS_SCHEMA,
            "supported": True,
            "enabled": catalog.enabled,
            "revision": catalog.revision,
            "count": len(catalog.assets),
            "charged_bytes": inventory.charged_bytes,
            "remnant_count": inventory.remnant_count,
            "protected": inventory.protected,
            "assets": [
                {
                    "asset_id": row.asset_id,
                    "byte_length": row.byte_length,
                    "width": row.width,
                    "height": row.height,
                    "frame_count": row.frame_count,
                    "duration_ms": row.duration_ms,
                    "created_at_ms": row.created_at_ms,
                    "closed_at_ms": row.closed_at_ms,
                    "referenced": bool(row.project_references),
                    "state": "expired"
                    if row.closed_at_ms is not None
                    and now - row.closed_at_ms >= CLOSED_RETENTION_MS
                    else "retained",
                }
                for row in catalog.assets
            ],
        }

    def restore_project_source(
        self, asset_id: str, *, deadline: float, cancelled: Callable[[], bool] = lambda: False
    ) -> RetainedAssetUse:
        """Fresh qualified backend use for an explicit project relink only."""
        require_asset_id(asset_id)
        check_retention_budget(deadline, cancelled)
        with self._lock:
            owner = self.qualify()
            store = self._qualified_store(owner)
            with self._lease():
                adapter = self._media_runtime()
                if type(adapter) is not QualifiedAVMediaAdapter:
                    raise RetainedAssetError("media_unqualified")
                use = store.restore(
                    asset_id,
                    media_adapter=adapter,
                    scratch_root=adapter._scratch_root,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                try:
                    self._qualified_store(owner)
                    check_retention_budget(deadline, cancelled)
                    return use
                except BaseException:
                    use.release()
                    raise

    def adopt_project_source(self, use: RetainedAssetUse, project_id: str) -> None:
        with self._lock:
            self._qualified_store(self._owner).adopt_project_use(use, project_id)

    def sync_recovery_references(
        self,
        project_id: str,
        asset_ids: tuple[str, ...],
        *,
        uses: tuple[RetainedAssetUse, ...] = (),
    ) -> None:
        with self._lock:
            self._qualified_store(self._owner).sync_recovery_references(
                project_id, asset_ids, uses=uses
            )

    def dispatch(
        self,
        action: dict[str, object],
        *,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
        admitted_owner: RecoveryOwner | None = None,
    ) -> dict[str, Any]:
        from .media_runtime_manager import MediaRuntimeBusy

        command = decode_retained_action(json_bytes(action))
        check_retention_budget(deadline, cancelled)
        with self._lock:
            store = self._qualified_store(admitted_owner)
            if command["intent"] == "release":
                # IMPORTANT: revocation owns only the fresh process resource. Broken catalog
                # metadata must not prevent cleanup; its later status read may still refuse.
                store.release_use(cast(str, command["use_handle"]))
            catalog = store.read()
            if "expected_revision" in command and command["expected_revision"] != catalog.revision:
                raise RetainedAssetError("revision_conflict")
            intent, retained_id, restored, cleanup = command["intent"], None, None, None
            use = None
            try:
                if intent == "set_enabled":
                    store.set_enabled(
                        cast(bool, command["enabled"]), expected_revision=catalog.revision
                    )
                elif intent in {"clear", "collect"}:
                    result = (store.clear if intent == "clear" else store.collect)(
                        expected_revision=catalog.revision
                    )
                    cleanup = {"removed": result.removed, "protected": result.protected}
                elif intent in {"retain", "restore"}:
                    if not catalog.enabled:
                        raise RetainedAssetError("recovery_disabled")
                    claim = None
                    if intent == "retain":
                        production = self._production()
                        if type(production) is not ProductionWorkspaceRegistry:
                            raise RetainedAssetError("source_unavailable")
                        # IMPORTANT: the exact business claim is captured before media IO; no
                        # Production lock may span activation, probing, streaming or catalog CAS.
                        claim = production.claim_authoring_output_batch(
                            workspace_handle=cast(str, command["workspace_handle"]),
                            workspace_id=cast(str, command["workspace_id"]),
                            expected_workspace_revision=cast(
                                int, command["expected_workspace_revision"]
                            ),
                            expected_workspace_fingerprint=cast(
                                str, command["expected_workspace_fingerprint"]
                            ),
                            pairs=(
                                (
                                    cast(str, command["segment_id"]),
                                    cast(str, command["output_handle"]),
                                ),
                            ),
                        )[0]
                    with self._lease():
                        adapter = self._media_runtime()
                        check_retention_budget(deadline, cancelled)
                        self._qualified_store(admitted_owner)
                        if type(adapter) is not QualifiedAVMediaAdapter:
                            raise RetainedAssetError("media_unqualified")
                        if claim is not None:
                            source = stage_retention_source(
                                claim=claim,
                                owner_id=store.owner_id,
                                media_adapter=adapter,
                                scratch_root=adapter._scratch_root,
                                deadline=deadline,
                                cancelled=cancelled,
                            )
                            try:
                                _, asset = store.admit(
                                    source,
                                    expected_revision=catalog.revision,
                                    deadline=deadline,
                                    cancelled=cancelled,
                                )
                                retained_id = asset.asset_id
                            finally:
                                source.release()
                        else:
                            use = store.restore(
                                cast(str, command["asset_id"]),
                                media_adapter=adapter,
                                scratch_root=adapter._scratch_root,
                                deadline=deadline,
                                cancelled=cancelled,
                            )
                            facts = use.source.facts
                            restored = {
                                "asset_id": use.asset_id,
                                "use_handle": use.use_handle,
                                "width": facts.width,
                                "height": facts.height,
                                "frame_count": facts.frame_count,
                                "preview_available": _native_preview_fps(facts) is not None,
                            }
                self._qualified_store(admitted_owner)
                check_retention_budget(deadline, cancelled)
                response = {
                    "schema": RESPONSE_SCHEMA,
                    "projection": self._projection(store.inventory()),
                    "error": None,
                    "retained_id": retained_id,
                    "restored": restored,
                    "cleanup": cleanup,
                }
                self._qualified_store(admitted_owner)
                check_retention_budget(deadline, cancelled)
                return response
            except BaseException as error:
                if use is not None:
                    use.release()
                if isinstance(error, ProductionWorkbenchError):
                    raise RetainedAssetError("source_stale") from None
                if isinstance(
                    error, (AuthoringSourceBindingError, AVMediaAdapterError, MediaRuntimeBusy)
                ):
                    raise _media_refusal(error) from None
                raise

    def discard_response(self, response: dict[str, Any]) -> None:
        """The route drops only a fresh resource from its abandoned worker result."""
        restored = response.get("restored")
        if type(restored) is dict and type(restored.get("use_handle")) is str:
            with self._lock:
                if self._store is not None:
                    self._store.release_use(restored["use_handle"])

    def preview(
        self,
        use_handle: str,
        *,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
        admitted_owner: RecoveryOwner | None = None,
    ) -> tuple[bytearray, str]:
        from .media_runtime_manager import MediaRuntimeBusy

        check_retention_budget(deadline, cancelled)
        with self._lock:
            use = self._qualified_store(admitted_owner).claim_use(_use_handle(use_handle))
            source = claim_transferred_authoring_source(use.receipt, source_id=use.source_id)
            borrower = use.source.borrow_for_render()
        body: bytearray | None = None
        try:
            facts = use.source.facts
            fps = _native_preview_fps(facts)
            if fps is None:
                raise RetainedAssetError("media_unqualified")
            with self._lease():
                adapter = self._media_runtime()
                body, _ = execute_transferred_authoring_preview(
                    source,
                    adapter,
                    source_start_frame=0,
                    frames=facts.frame_count,
                    source_fps=fps,
                    deadline=deadline,
                    cancellation=_Cancellation(cancelled),
                )
            with self._lock:
                self._qualified_store(admitted_owner)
                check_retention_budget(deadline, cancelled)
                if not use.source.current():
                    raise RetainedAssetError("source_stale")
            return body, facts.embedded_audio.disposition
        except (AuthoringSourceBindingError, AVMediaAdapterError, MediaRuntimeBusy) as error:
            if body is not None:
                body.clear()
            raise _media_refusal(error) from None
        except BaseException:
            if body is not None:
                body.clear()
            raise
        finally:
            borrower.release()


def build_retained_asset_service() -> RetainedAssetService:
    from .composition_root import PRODUCTION_WORKSPACE, get

    service = RetainedAssetService(
        owner_port=RecoveryOwnerPort(),
        production=lambda: cast(ProductionWorkspaceRegistry, get(PRODUCTION_WORKSPACE)),
    )
    atexit.register(service.close)
    return service
