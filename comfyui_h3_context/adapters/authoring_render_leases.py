"""Job-owned, non-serializable source handoff; never an alternate source factory."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import NoReturn, SupportsIndex

from ..core.authoring_render_jobs import RenderJobLimits
from ..core.composition_contract import PublicCompositionSnapshot
from ..core.registry import ReferenceRegistry
from ..core.render_planner import RenderPlannerError, SourceCurrentnessConfirmation
from .authoring_fonts import AuthoringFontError, PackagedFontManifest, load_packaged_font_manifest
from .authoring_generated_source import (
    GeneratedAuthoringVideoBorrower,
    GeneratedAuthoringVideoSource,
)
from .authoring_image_source import IMAGE_SOURCE_PROFILE, OwnedImageSource
from .authoring_render_source import (
    AuthoringRenderSourceClaim,
    BoundAuthoringRenderPlan,
    SourceOrigin,
    prepare_bound_render_plan,
)
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    _PathBackedAuthoringVideoSource,
    claim_transferred_authoring_source,
)

_DEFAULT_LIMITS = RenderJobLimits()


class RenderSourceLeaseError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {
            "source_released",
            "source_expired",
            "source_replaced",
            "source_unavailable",
            "source_not_found",
            "workspace_released",
            "cancelled",
            "service_closed",
            "resource_limit",
            "plan_mismatch",
            "font_changed",
            "invalid_request",
        }:
            code = "source_unavailable"
        self.code = code
        super().__init__(code)


@dataclass(slots=True, repr=False)
class _JobSource:
    fingerprint: str
    size: int
    pixels: bytes | None
    video: _PathBackedAuthoringVideoSource | None
    generated: GeneratedAuthoringVideoBorrower | None


class AuthoringRenderJobSources:
    """One running job owns the bytes, time limit and exact invocation identities."""

    def __init__(
        self,
        *,
        sources: dict[str, _JobSource],
        invocations: tuple[tuple[AuthoringSourceBindingReceipt, ReferenceRegistry], ...],
        confirmation: SourceCurrentnessConfirmation,
        fonts: PackagedFontManifest,
        deadline: float,
        clock: Callable[[], float],
        cancelled: threading.Event,
    ) -> None:
        self._sources = sources
        self._invocations = invocations
        self._confirmation = confirmation
        self._fonts = fonts
        self._deadline = deadline
        self._clock = clock
        self._cancelled = cancelled
        self._released = threading.Event()
        self._lock = threading.RLock()
        self._failure: str | None = None

    def __repr__(self) -> str:
        return "<AuthoringRenderJobSources opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("render source leases are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("render source leases are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("render source leases are not serializable")

    @property
    def source_bytes(self) -> int:
        with self._lock:
            return sum(source.size for source in self._sources.values())

    def is_cancelled(self) -> bool:
        return (
            self._released.is_set()
            or self._cancelled.is_set()
            or any(
                receipt._render_invalidated is not None for receipt, _registry in self._invocations
            )
        )

    def _guard(self) -> None:
        reason = self._failure
        now = self._clock()
        if reason is None and self._released.is_set():
            reason = "source_released"
        if reason is None and self._cancelled.is_set():
            reason = "cancelled"
        if reason is None and (not math.isfinite(now) or now >= self._deadline):
            reason = "source_expired"
        if reason is None:
            reason = next(
                (
                    receipt._render_invalidated
                    for receipt, _ in self._invocations
                    if receipt._render_invalidated is not None
                ),
                None,
            )
        if reason is not None:
            self._failure = reason
            self.release()
            raise RenderSourceLeaseError(reason) from None

    def release(self) -> None:
        # Signal before the lock: an in-flight bounded file read must observe release, not
        # keep doing I/O while teardown waits to reclaim its accumulator.
        self._released.set()
        with self._lock:
            for source in self._sources.values():
                if source.video is not None:
                    source.video.release()
                if source.generated is not None:
                    source.generated.release()
                source.pixels = None
            self._sources.clear()
            for receipt, _registry in self._invocations:
                with receipt._lifecycle_lock:
                    receipt._render_borrowers -= 1
            self._invocations = ()

    def read_source(self, asset_id: str) -> bytes:
        with self._lock:
            self._guard()
            if type(asset_id) is not str or asset_id not in self._sources:
                raise RenderSourceLeaseError("source_not_found")
            source = self._sources[asset_id]
            if source.pixels is not None:
                result = source.pixels
            else:
                result = self._read_video(source)
            self._guard()
            return result

    def _read_video(self, source: _JobSource) -> bytes:
        from .av_reconstruction_media import AVMediaAdapterError, _read_regular_media_body

        video = source.video
        generated = source.generated
        if video is not None and video.current() and video._path is not None:
            source_path = video._path
            current = video.current
        elif generated is not None and generated.current() and generated.path is not None:
            source_path = generated.path
            current = generated.current
        else:
            self._failure = "source_replaced"
            self._guard()
            raise RenderSourceLeaseError("source_replaced")
        body: bytearray | None = None
        try:
            body, fingerprint = _read_regular_media_body(
                source_path,
                source.size,
                deadline=self._deadline,
                cancellation=self,
                clock=self._clock,
            )
            self._guard()
            # CRITICAL: equal stat metadata is not equal content. Rehash every private read
            # and recheck the original identity after it, including reads for publication.
            if not current() or fingerprint != source.fingerprint or len(body) != source.size:
                self._failure = "source_replaced"
                self._guard()
            return bytes(body)
        except AVMediaAdapterError:
            self._guard()
            self._failure = "source_replaced"
            self._guard()
            raise RenderSourceLeaseError("source_replaced") from None
        finally:
            if body is not None:
                body.clear()

    def confirm_currentness(self) -> SourceCurrentnessConfirmation:
        with self._lock:
            self._guard()
            try:
                if load_packaged_font_manifest() != self._fonts:
                    self._failure = "font_changed"
            except (AuthoringFontError, OSError, ValueError):
                self._failure = "font_changed"
            self._guard()
            for asset_id in tuple(self._sources):
                self.read_source(asset_id)
            self._guard()
            return self._confirmation


def _transfer_source(claim: AuthoringRenderSourceClaim, limits: RenderJobLimits) -> _JobSource:
    if type(claim) is not AuthoringRenderSourceClaim or not claim.current():
        raise RenderSourceLeaseError("source_unavailable")
    if not 1 <= claim.byte_count <= limits.max_source_bytes or (
        claim.duration_milliseconds is not None
        and not 1 <= claim.duration_milliseconds <= limits.max_source_duration_ms
    ):
        raise RenderSourceLeaseError("resource_limit")
    source = claim_transferred_authoring_source(claim._receipt, claim.asset.asset_id)
    if type(source) is OwnedImageSource and claim.origin is SourceOrigin.RUNTIME_IMAGE:
        pixels = source.read_bytes()
        digest = hashlib.sha256()
        digest.update(f"{IMAGE_SOURCE_PROFILE}:{source.width}:{source.height}:".encode("ascii"))
        digest.update(pixels)
        if (
            "sha256:" + digest.hexdigest() != claim.source_fingerprint
            or len(pixels) != claim.byte_count
            or (source.width, source.height) != (claim.width, claim.height)
        ):
            raise RenderSourceLeaseError("source_replaced")
        return _JobSource(claim.source_fingerprint, len(pixels), pixels, None, None)
    if (
        type(source) is _PathBackedAuthoringVideoSource
        and claim.origin is SourceOrigin.CONTEXT_VIDEO
    ):
        if not source.current() or source._root is None or source._path is None:
            raise RenderSourceLeaseError("source_replaced")
        # This duplicates only a factory-admitted identity, never a caller locator. The original
        # receipt may release its wrapper after handoff without erasing this job's verifier.
        owned = _PathBackedAuthoringVideoSource(
            root=source._root,
            path=source._path,
            identity=source._identity,
            maximum_bytes=min(source._maximum_bytes, limits.max_source_bytes),
        )
        return _JobSource(claim.source_fingerprint, claim.byte_count, None, owned, None)
    if type(source) is GeneratedAuthoringVideoSource and claim.origin is SourceOrigin.GENERATED:
        # IMPORTANT: transfer a borrower, not the workspace owner.  A running render may outlive
        # Authoring release, while Production revocation still invalidates every subsequent read.
        borrower = source.borrow_for_render()
        return _JobSource(claim.source_fingerprint, claim.byte_count, None, None, borrower)
    raise RenderSourceLeaseError("source_unavailable")


def acquire_render_job_sources(
    bound: BoundAuthoringRenderPlan,
    *,
    deadline: float,
    limits: RenderJobLimits = _DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
    cancelled: threading.Event | None = None,
) -> AuthoringRenderJobSources:
    if (
        type(bound) is not BoundAuthoringRenderPlan
        or type(limits) is not RenderJobLimits
        or type(deadline) is not float
        or not math.isfinite(deadline)
        or not callable(clock)
        or (cancelled is not None and type(cancelled) is not threading.Event)
    ):
        raise RenderSourceLeaseError("invalid_request")
    stop = cancelled if cancelled is not None else threading.Event()
    now = clock()
    if stop.is_set():
        raise RenderSourceLeaseError("cancelled")
    if not math.isfinite(now) or deadline <= now:
        raise RenderSourceLeaseError("source_expired")
    cutoff = min(deadline, now + limits.source_lease_seconds, now + limits.deadline_ms / 1000)
    job: AuthoringRenderJobSources | None = None
    sources: dict[str, _JobSource] = {}
    invocations: list[tuple[AuthoringSourceBindingReceipt, ReferenceRegistry]] = []
    try:
        snapshot = bound._issued_snapshot
        if type(snapshot) is not PublicCompositionSnapshot:
            raise RenderSourceLeaseError("plan_mismatch")
        # A current source confirmation authenticates no caller-replaced operations. Re-run
        # the canonical planner for the factory-retained snapshot before acquiring anything.
        rebuilt = prepare_bound_render_plan(
            bound._history, snapshot, bound._workspace_is_current, deadline=cutoff
        )
        if rebuilt.plan != bound.plan:
            raise RenderSourceLeaseError("plan_mismatch")
        claims = bound._history.sources
        receipts = sorted({id(claim._receipt): claim._receipt for claim in claims}.values(), key=id)
        with ExitStack() as stack:
            for receipt in receipts:
                stack.enter_context(receipt._lifecycle_lock)
                if receipt._render_invalidated is not None:
                    raise RenderSourceLeaseError(receipt._render_invalidated)
            confirmation = bound.confirm_currentness(deadline=cutoff)
            if sum(claim.byte_count for claim in claims) > limits.max_source_set_bytes:
                raise RenderSourceLeaseError("resource_limit")
            for claim in claims:
                if claim.asset.asset_id in sources:
                    raise RenderSourceLeaseError("plan_mismatch")
                sources[claim.asset.asset_id] = _transfer_source(claim, limits)
            for receipt in receipts:
                if receipt._exact_registry is None or receipt._render_borrowers >= limits.max_jobs:
                    raise RenderSourceLeaseError("resource_limit")
                invocations.append((receipt, receipt._exact_registry))
            bound.confirm_currentness(deadline=cutoff)
            for receipt, _registry in invocations:
                receipt._render_borrowers += 1
            job = AuthoringRenderJobSources(
                sources=sources,
                invocations=tuple(invocations),
                confirmation=confirmation,
                fonts=bound._history.fonts,
                deadline=cutoff,
                clock=clock,
                cancelled=stop,
            )
            try:
                job.confirm_currentness()
            except BaseException:
                job.release()
                raise
            return job
    except (AuthoringSourceBindingError, AuthoringFontError, RenderPlannerError) as exc:
        code = getattr(exc, "code", "source_unavailable")
        mapped = {
            "source_timeout": "source_expired",
            "source_stale": "workspace_released",
            "font_manifest_changed": "font_changed",
        }.get(code, code)
        raise RenderSourceLeaseError(mapped) from None
    finally:
        if job is None:
            for source in sources.values():
                if source.video is not None:
                    source.video.release()
                if source.generated is not None:
                    source.generated.release()
                source.pixels = None
            sources.clear()
