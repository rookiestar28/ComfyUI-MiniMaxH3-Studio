"""Process-local public handles over the existing private Authoring renderer and store.

The workspace port supplies live authority. No caller receipt, path or render result is admitted.
Complete-byte reads stay with the store; this adapter owns bounded transport leases, not files.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from ..core.authoring_output_protocol import (
    OUTPUT_MAX_BYTES,
    OUTPUT_MAX_FRAMES,
    OUTPUT_RESPONSE_CHUNK,
    OUTPUT_RESPONSE_SECONDS,
    OUTPUT_STATUS_SCHEMA,
    OUTPUT_TTL_SECONDS,
    PREVIEW_MAX_BYTES,
    AuthoringOutputCreate,
    OutputByteRange,
    OutputProtocolError,
    output_media_headers,
    require_output_handle,
    require_output_workspace,
    select_output_range,
)
from ..core.authoring_render_jobs import (
    AuthoringRenderJobRequestV1,
    RenderJobPhase,
    RenderJobState,
    request_for_render_plan,
)
from ..core.authoring_render_receipts import AuthoringRenderReceiptV1, MeasuredRenderOutput
from ..core.composition_contract import OUTPUT_PROFILE_ID, decode_public_snapshot
from ..core.nle_authoring_contract import TIMELINE_HISTORY_PROJECTION_SCHEMA_V2
from .authoring_output_preview import validate_preview
from .authoring_render_service import AuthoringRenderService, RenderServiceError
from .authoring_render_source import BoundAuthoringRenderPlan
from .authoring_render_store import RenderOutputStore, RenderStoreError
from .authoring_source_binding import AuthoringSourceBindingError
from .comfyui_authoring_workspace import AuthoringDispatchResult, AuthoringWorkbenchError


class OutputWorkspace(Protocol):
    def dispatch(self, action: dict[str, object]) -> AuthoringDispatchResult: ...

    def prepare_render_plan(self, handle: str) -> BoundAuthoringRenderPlan: ...


class OutputPreviewBackend(Protocol):
    def render(
        self, body: bytes, parent: MeasuredRenderOutput, check: Callable[[], None]
    ) -> tuple[bytes, MeasuredRenderOutput]: ...

    def close(self) -> None: ...


@dataclass(slots=True, repr=False)
class _OutputEntry:
    public: AuthoringOutputCreate
    request: AuthoringRenderJobRequestV1
    job_handle: str
    state: RenderJobState
    expires: float
    reference_revision: int
    output_handle: str | None = None
    summary: dict[str, object] | None = None


class OutputResponseLease:
    """One immutable, completely verified body; no private pathname escapes the store."""

    def __init__(
        self,
        body: bytes,
        selection: OutputByteRange,
        *,
        check: Callable[[], None],
        release: Callable[[], None],
        preview: bool = False,
    ) -> None:
        if type(body) is not bytes or len(body) != selection.complete_length:
            raise OutputProtocolError("unavailable")
        self.selection = selection
        self.headers = output_media_headers(selection, preview=preview)
        self._body: bytes | None = body
        self._check = check
        self._release = release
        self._lock = threading.RLock()
        self._consumed = False

    def __repr__(self) -> str:
        return "<AuthoringOutputResponseLease opaque>"

    def __enter__(self) -> OutputResponseLease:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def chunks(self) -> Iterator[memoryview]:
        with self._lock:
            if self._body is None or self._consumed:
                raise OutputProtocolError("unavailable")
            self._consumed = True
        for start in range(self.selection.start, self.selection.stop, OUTPUT_RESPONSE_CHUNK):
            self._check()
            with self._lock:
                if self._body is None:
                    raise OutputProtocolError("unavailable")
                block = memoryview(self._body)[
                    start : min(start + OUTPUT_RESPONSE_CHUNK, self.selection.stop)
                ]
            yield block

    def close(self) -> None:
        with self._lock:
            if self._body is None:
                return
            self._body = None
            self._release()


class AuthoringOutputRegistry:
    """An explicit owner of one render service and its public transport lifecycle."""

    def __init__(
        self,
        *,
        workspace: OutputWorkspace,
        service: AuthoringRenderService,
        store: RenderOutputStore,
        preview: OutputPreviewBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        token: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        if type(service) is not AuthoringRenderService or type(store) is not RenderOutputStore:
            raise OutputProtocolError("runtime_unavailable")
        self._workspace = workspace
        self._service = service
        self._store = store
        self._preview_backend = preview
        self._previews: dict[str, tuple[bytes, MeasuredRenderOutput]] = {}
        self._stopped = threading.Event()
        self._clock = clock
        self._token = token
        self._lock = threading.RLock()
        self._bytes_gate = threading.BoundedSemaphore(1)
        self._active_responses = 0
        self._entries: dict[str, _OutputEntry] = {}
        self._tombstones: OrderedDict[str, _OutputEntry] = OrderedDict()
        self._outputs: dict[str, str] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self._leases: set[OutputResponseLease] = set()
        self._closed = False

    def __repr__(self) -> str:
        return "<AuthoringOutputRegistry opaque>"

    @property
    def active_responses(self) -> int:
        with self._lock:
            return self._active_responses

    @property
    def busy(self) -> bool:
        """Jobs, retained outputs or open responses that closing this registry would discard."""

        with self._lock:
            self._prune(self._now())
            return bool(self._entries or self._active_responses or self._leases) or (
                self._service.job_count > 0
            )

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise OutputProtocolError("unavailable")
        return float(value)

    def _open(self) -> None:
        if self._closed:
            raise OutputProtocolError("unavailable")

    def _forget(self, entry: _OutputEntry) -> None:
        self._keys.pop((entry.public.workspace_handle, entry.public.idempotency_key), None)
        if entry.output_handle is not None:
            self._outputs.pop(entry.output_handle, None)

    def _prune(self, now: float) -> None:
        for handle, entry in tuple(self._entries.items()):
            if now >= entry.expires:
                del self._entries[handle]
                self._tombstones[handle] = entry
                self._previews.pop(handle, None)
        for handle, entry in tuple(self._tombstones.items()):
            if now >= entry.expires + 600:
                del self._tombstones[handle]
                self._forget(entry)
        while len(self._tombstones) > 64:
            _, entry = self._tombstones.popitem(last=False)
            self._forget(entry)

    def _mint(self, kind: Literal["job", "output"]) -> str:
        prefix = "arj_" if kind == "job" else "aro_"
        for _attempt in range(4):
            value = self._token()
            if type(value) is not str:
                raise OutputProtocolError("resource_limit")
            handle = prefix + value
            require_output_handle(handle, kind=kind)
            if (
                handle not in self._entries
                and handle not in self._tombstones
                and handle not in self._outputs
            ):
                return handle
        raise OutputProtocolError("resource_limit")

    def _read_currency(self, request: AuthoringOutputCreate) -> tuple[bool, int]:
        self._open()
        try:

            def read(action: str) -> dict[str, object]:
                result = self._workspace.dispatch(
                    {
                        "request_id": "output-" + uuid.uuid4().hex,
                        "action": action,
                        "payload": {"workspace_handle": request.workspace_handle},
                    }
                )
                if result.status != 200 or type(result.body) is not dict:
                    raise OutputProtocolError("unavailable")
                return result.body

            reference = read("read_projection").get("reference")
            if type(reference) is not dict or type(reference.get("revision")) is not int:
                raise OutputProtocolError("unavailable")
            history = read("read_timeline_history")
            snapshot_wire = history.get("snapshot")
            if snapshot_wire is None:
                if history.get("schema") != TIMELINE_HISTORY_PROJECTION_SCHEMA_V2:
                    raise OutputProtocolError("unavailable")
                # IMPORTANT: an empty V2 authoring state has no render snapshot. Only consume its
                # materialized snapshot when content exists; synthesizing one from edit capacity
                # would turn unused capacity into exported media.
                snapshot_wire = history.get("render_snapshot")
            if not isinstance(snapshot_wire, Mapping):
                raise OutputProtocolError("unavailable")
            snapshot = decode_public_snapshot(dict(snapshot_wire))
            if snapshot.workspace_handle != request.workspace_handle:
                raise OutputProtocolError("unavailable")
            # IMPORTANT: retained output currency reads workspace/history only. Recapturing
            # source leases here would revoke complete old outputs when their inputs expire.
            current = (
                snapshot.workspace_revision == request.workspace_revision
                and snapshot.timeline_revision == request.timeline_revision
                and snapshot.public_fingerprint == request.snapshot_fingerprint
            )
            return current, reference["revision"]
        except OutputProtocolError:
            raise
        except Exception:
            raise OutputProtocolError("unavailable") from None

    def _currency(self, request: AuthoringOutputCreate, reference_revision: int) -> str:
        # CRITICAL: history transactions advance workspace revision independently of references.
        # Comparing those counters rejects valid edits; pin and recheck each authority separately.
        current, reference = self._read_currency(request)
        return "current" if current and reference == reference_revision else "old_revision"

    def _lookup(self, handle: str, workspace_handle: str, *, output: bool = False) -> _OutputEntry:
        self._open()
        require_output_workspace(workspace_handle)
        require_output_handle(handle, kind="output" if output else "job")
        self._prune(self._now())
        key = self._outputs.get(handle) if output else handle
        entry = (self._entries.get(key) or self._tombstones.get(key)) if key is not None else None
        if entry is None or entry.public.workspace_handle != workspace_handle:
            raise OutputProtocolError("unavailable")
        self._currency(entry.public, entry.reference_revision)
        return entry

    def _reserve(self) -> None:
        if not self._bytes_gate.acquire(blocking=False):
            raise OutputProtocolError("resource_limit")
        self._active_responses += 1

    def _release(self) -> None:
        self._active_responses -= 1
        self._bytes_gate.release()

    def _body(self, entry: _OutputEntry) -> tuple[bytes, AuthoringRenderReceiptV1]:
        if self._now() >= entry.expires:
            raise OutputProtocolError("expired")
        try:
            state = self._service.status(entry.state.job_id, entry.request.fingerprint)
            entry.state = state
            if state.phase is not RenderJobPhase.SUCCEEDED:
                raise OutputProtocolError("not_ready")
            artifact = self._service.artifact(state.job_id, entry.request.fingerprint)
            receipt = artifact.receipt
            if (
                receipt.request != entry.request
                or receipt.fingerprint != state.receipt_fingerprint
                or not 1 <= receipt.observed.byte_length <= OUTPUT_MAX_BYTES
                or not 1 <= receipt.observed.frame_count <= OUTPUT_MAX_FRAMES
                or receipt.observed.audio_streams not in {0, 1}
            ):
                raise OutputProtocolError("unavailable")
            # CRITICAL: verify the complete output before selecting ANY range. Trusting just
            # a requested prefix would serve bytes from an artifact whose unseen tail changed.
            body = self._store.read_output(state.job_id, entry.request.fingerprint)
            self._currency(entry.public, entry.reference_revision)
            if self._now() >= entry.expires:
                raise OutputProtocolError("expired")
            return body, receipt
        except (RenderServiceError, RenderStoreError):
            raise OutputProtocolError("unavailable") from None

    @staticmethod
    def _summary(receipt: AuthoringRenderReceiptV1) -> dict[str, object]:
        facts = receipt.observed
        return {
            "output_fingerprint": facts.output_fingerprint,
            "byte_length": facts.byte_length,
            "width": facts.width,
            "height": facts.height,
            "frame_count": facts.frame_count,
            "frame_rate_num": facts.frame_rate_num,
            "frame_rate_den": facts.frame_rate_den,
            "audio_streams": facts.audio_streams,
            "output_profile_id": OUTPUT_PROFILE_ID,
            "verified": True,
        }

    def _status(self, entry: _OutputEntry) -> dict[str, object]:
        currency = self._currency(entry.public, entry.reference_revision)
        availability = "gone"
        if self._now() >= entry.expires:
            availability = "expired"
        else:
            try:
                entry.state = self._service.status(entry.state.job_id, entry.request.fingerprint)
                if entry.state.phase is RenderJobPhase.SUCCEEDED:
                    self._reserve()
                    try:
                        _body, receipt = self._body(entry)
                        entry.summary = self._summary(receipt)
                        if entry.output_handle is None:
                            entry.output_handle = self._mint("output")
                            self._outputs[entry.output_handle] = entry.job_handle
                        availability = "available"
                    finally:
                        self._release()
            except (RenderServiceError, RenderStoreError):
                availability = "gone"
            except OutputProtocolError as exc:
                if exc.code == "resource_limit":
                    raise
                availability = "expired" if exc.code == "expired" else "gone"
        currency = self._currency(entry.public, entry.reference_revision)
        state = entry.state
        request = entry.public
        return {
            "schema": OUTPUT_STATUS_SCHEMA,
            "job_handle": entry.job_handle,
            "output_handle": entry.output_handle,
            "workspace_handle": request.workspace_handle,
            "workspace_revision": request.workspace_revision,
            "timeline_revision": request.timeline_revision,
            "snapshot_fingerprint": request.snapshot_fingerprint,
            "state_version": state.version,
            "phase": state.phase.value,
            "progress_bp": state.progress_bp,
            "failure": None if state.failure is None else state.failure.value,
            "currency": currency,
            "availability": availability,
            "output": dict(entry.summary) if entry.summary is not None else None,
        }

    def create(self, request: AuthoringOutputCreate) -> dict[str, object]:
        if type(request) is not AuthoringOutputCreate:
            raise OutputProtocolError()
        with self._lock:
            self._open()
            now = self._now()
            self._prune(now)
            key = (request.workspace_handle, request.idempotency_key)
            previous = self._keys.get(key)
            if previous is not None:
                entry = self._lookup(previous, request.workspace_handle)
                if entry.public != request:
                    raise OutputProtocolError("idempotency_conflict")
                if now >= entry.expires:
                    raise OutputProtocolError("expired")
                return self._status(entry)
            if len(self._entries) >= 32:
                raise OutputProtocolError("resource_limit")
            current, reference_revision = self._read_currency(request)
            if not current:
                raise OutputProtocolError("revision_conflict")
            try:
                bound = self._workspace.prepare_render_plan(request.workspace_handle)
                internal = request_for_render_plan(
                    bound.plan, idempotency_key=request.idempotency_key, timeout_ms=900_000
                )
                if (
                    internal.workspace_handle != request.workspace_handle
                    or internal.workspace_revision != request.workspace_revision
                    or internal.timeline_revision != request.timeline_revision
                    or internal.snapshot_fingerprint != request.snapshot_fingerprint
                    or bound.plan.output_profile.profile_id != request.output_profile_id
                ):
                    raise OutputProtocolError("revision_conflict")
                handle = self._mint("job")
                if self._currency(request, reference_revision) != "current":
                    raise OutputProtocolError("revision_conflict")
                state = self._service.submit(internal, bound)
            except RenderServiceError as exc:
                code = (
                    exc.code
                    if exc.code in {"resource_limit", "idempotency_conflict", "runtime_unavailable"}
                    else "unavailable"
                )
                raise OutputProtocolError(code) from None
            except AuthoringWorkbenchError:
                raise OutputProtocolError("unavailable") from None
            except AuthoringSourceBindingError:
                # IMPORTANT (B-M2522-REVOKE-01): planning refuses a revoked source (for example an
                # imported Production original after its workspace release) before submission. It
                # is the same lost authority `submit` reports as `source_unavailable`; uncaught it
                # became an untyped 500 and left the browser polling a workspace it cannot render.
                raise OutputProtocolError("unavailable") from None
            entry = _OutputEntry(
                request, internal, handle, state, now + OUTPUT_TTL_SECONDS, reference_revision
            )
            self._entries[handle] = entry
            self._keys[key] = handle
            return self._status(entry)

    def status(self, handle: str, workspace_handle: str) -> dict[str, object]:
        with self._lock:
            return self._status(self._lookup(handle, workspace_handle))

    def cancel(self, handle: str, workspace_handle: str) -> dict[str, object]:
        with self._lock:
            entry = self._lookup(handle, workspace_handle)
            if self._now() >= entry.expires:
                raise OutputProtocolError("expired")
            try:
                entry.state = self._service.cancel(entry.state.job_id, entry.request.fingerprint)
            except RenderServiceError:
                raise OutputProtocolError("unavailable") from None
            return self._status(entry)

    def open_download(
        self, handle: str, workspace_handle: str, *, range_header: object = None
    ) -> OutputResponseLease:
        with self._lock:
            entry = self._lookup(handle, workspace_handle, output=True)
            self._reserve()
            try:
                deadline = self._now() + OUTPUT_RESPONSE_SECONDS
                body, _receipt = self._body(entry)
                selection = select_output_range(range_header, len(body))

                def check() -> None:
                    with self._lock:
                        self._open()
                        if self._now() >= min(deadline, entry.expires):
                            raise OutputProtocolError("expired")
                        self._currency(entry.public, entry.reference_revision)
                        try:
                            self._service.status(entry.state.job_id, entry.request.fingerprint)
                        except RenderServiceError:
                            raise OutputProtocolError("unavailable") from None

                def release() -> None:
                    with self._lock:
                        self._leases.discard(lease)
                        self._release()

                check()
                lease = OutputResponseLease(body, selection, check=check, release=release)
                self._leases.add(lease)
                return lease
            except BaseException:
                self._release()
                raise

    def open_preview(
        self,
        handle: str,
        workspace_handle: str,
        *,
        range_header: object = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> OutputResponseLease:
        with self._lock:
            entry = self._lookup(handle, workspace_handle, output=True)
            backend = self._preview_backend
            if backend is None:
                raise OutputProtocolError("preview_unavailable")
            self._reserve()
            deadline = self._now() + OUTPUT_RESPONSE_SECONDS

        def check() -> None:
            if self._stopped.is_set() or cancelled():
                raise OutputProtocolError("preview_unavailable")
            with self._lock:
                self._open()
                if self._now() >= min(deadline, entry.expires):
                    raise OutputProtocolError("expired")
                self._currency(entry.public, entry.reference_revision)
                try:
                    self._service.status(entry.state.job_id, entry.request.fingerprint)
                except RenderServiceError:
                    raise OutputProtocolError("unavailable") from None

        try:
            with self._lock:
                # IMPORTANT: a cached derivative never replaces current parent authorization
                # and complete-byte verification, even after its original source lease expires.
                body, receipt = self._body(entry)
                cached = self._previews.get(entry.job_handle)
                if cached is None and len(self._previews) >= 16:
                    raise OutputProtocolError("resource_limit")
            check()
            # Do not hold the registry lock through native work: revocation and shutdown must
            # reach the child's cancellation guard while encoding or probing is in progress.
            rendered = backend.render(body, receipt.observed, check) if cached is None else cached
            if type(rendered) is not tuple or len(rendered) != 2:
                raise OutputProtocolError("preview_unavailable")
            preview, facts = rendered
            validate_preview(preview, facts, receipt.observed)
            del body
            check()
            with self._lock:
                if cached is None:
                    if (
                        sum(len(value[0]) for value in self._previews.values()) + len(preview)
                        > 16 * PREVIEW_MAX_BYTES
                    ):
                        raise OutputProtocolError("resource_limit")
                    self._previews[entry.job_handle] = (preview, facts)
                selection = select_output_range(range_header, len(preview))

                def release() -> None:
                    with self._lock:
                        self._leases.discard(lease)
                        self._release()

                lease = OutputResponseLease(
                    preview, selection, check=check, release=release, preview=True
                )
                self._leases.add(lease)
                return lease
        except BaseException:
            with self._lock:
                self._release()
            raise

    def close(self) -> None:
        self._stopped.set()
        with self._lock:
            self._closed = True
            leases = tuple(self._leases)
        for lease in leases:
            lease.close()
        if self._preview_backend is not None:
            self._preview_backend.close()
        self._service.close()
        with self._lock:
            self._entries.clear()
            self._outputs.clear()
            self._keys.clear()
            self._tombstones.clear()
            self._previews.clear()
