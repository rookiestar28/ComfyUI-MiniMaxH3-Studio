"""Private generated VIDEO ownership for the M25-29 Authoring import bridge."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, SupportsIndex

from ..core.canonical import canonical_fingerprint
from ..core.composition_contract import (
    PublicAsset,
    Rational,
    TimingLandmark,
    composition_contract_fingerprint,
)
from ..core.errors import MediaProcessError
from ..core.registry import ReferenceRegistry
from ..core.segment_artifacts import SegmentArtifactReceipt
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
)
from .authoring_video_facts import (
    AuthoringVideoFacts,
    AuthoringVideoFactsError,
    probe_authoring_video_facts,
)
from .av_reconstruction_media import AVMediaAdapterError, QualifiedAVMediaAdapter
from .media_subprocess import OwnedOutputLease, create_output_lease
from .segment_artifact_store import (
    ArtifactIdentity,
    ArtifactInspectionStatus,
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)

GENERATED_AUTHORING_SOURCE_SCHEMA = "h3.context.authoring_source.generated.v1"


@dataclass(frozen=True, slots=True)
class _ImportCancellationProbe:
    cancelled: Callable[[], bool] = field(repr=False)

    def is_cancelled(self) -> bool:
        value = self.cancelled()
        if type(value) is not bool:
            raise TypeError("import cancellation result is invalid")
        return value


def _duration_milliseconds(facts: AuthoringVideoFacts) -> int:
    last = facts.landmarks[-1]
    base = facts.source_time_base
    return ((last.pts + last.duration_ticks) * base.num * 1000 + base.den - 1) // base.den


@dataclass(slots=True, repr=False)
class GeneratedAuthoringVideoSource:
    """One copied artifact whose authority remains borrowed from exact Production state."""

    lease: OwnedOutputLease = field(repr=False)
    facts: AuthoringVideoFacts
    expected_receipt: SegmentArtifactReceipt = field(repr=False)
    store: PrivateSegmentArtifactStore = field(repr=False)
    production_is_current: Callable[[], bool] = field(repr=False)
    _owner_released: bool = field(default=False, init=False, repr=False)
    _borrowers: int = field(default=0, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _artifact_identity: ArtifactIdentity | None = field(default=None, init=False, repr=False)

    def __repr__(self) -> str:
        return "<GeneratedAuthoringVideoSource opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("generated authoring sources are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("generated authoring sources are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("generated authoring sources are not serializable")

    @property
    def duration_milliseconds(self) -> int:
        return _duration_milliseconds(self.facts)

    def public_asset(self, asset_id: str) -> PublicAsset:
        audio = self.facts.embedded_audio
        return PublicAsset(
            asset_id,
            "video",
            Rational(self.facts.source_time_base.num, self.facts.source_time_base.den),
            self.facts.frame_count,
            audio.canonical_sample_count,
            audio.disposition,
            "nonnegative_monotonic_v1",
            tuple(
                TimingLandmark(row.frame_index, row.pts, row.dts, row.duration_ticks)
                for row in self.facts.landmarks
            ),
        )

    def source_profile_fingerprint(self, asset_id: str) -> str:
        return composition_contract_fingerprint(
            {
                "profile": self.facts.schema_version,
                "asset": self.public_asset(asset_id).to_wire(),
                "pixel_format": self.facts.pixel_format,
                "pixel_aspect": (
                    None
                    if self.facts.pixel_aspect_ratio is None
                    else self.facts.pixel_aspect_ratio.to_wire()
                ),
            }
        )

    def color_facts_fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "range": self.facts.color_range,
                "space": self.facts.color_space,
                "primaries": self.facts.color_primaries,
                "transfer": self.facts.color_transfer,
            }
        )

    def _artifact_current(self, *, prove: bool = False) -> bool:
        if self.lease.released:
            return False
        try:
            if not self.production_is_current():
                return False
            # IMPORTANT: the lease may hold the color/timestamp-normalized copy, so it is measured
            # against its own probed facts while the store inspection keeps the original
            # Production receipt. Using `expected_receipt.byte_length` here marks every
            # normalized import stale.
            status, size = self.lease.inspect_artifact(self.facts.byte_length)
            if status != "ok" or size != self.facts.byte_length:
                return False
            # CRITICAL: a full inspection reads and hashes the whole stored artifact. The workspace
            # owner's currentness is asked many times per lease and per reaper tick, so it is
            # answered from the identity the last full inspection recorded; `verify_artifact`
            # (every `confirm`), a render borrower and a missing or changed identity pay for the
            # proof. Making this unconditional again costs one artifact hash per question; dropping
            # the proof from `verify_artifact` lets a same-size replacement keep media authority.
            identity = self._artifact_identity
            if (
                not prove
                and identity is not None
                and self.store.identity_current(self.expected_receipt, identity)
            ):
                return True
            self._artifact_identity = None
            inspection, identity = self.store.inspect_with_identity(
                self.expected_receipt, maximum_bytes=self.expected_receipt.byte_length
            )
            if inspection.status is not ArtifactInspectionStatus.REUSABLE:
                return False
            self._artifact_identity = identity
            return True
        except Exception:
            return False

    def current(self) -> bool:
        with self._lock:
            return not self._owner_released and self._artifact_current()

    def verify_artifact(self) -> bool:
        """`current()` with the stored artifact's content proven now, not taken from identity."""

        with self._lock:
            return not self._owner_released and self._artifact_current(prove=True)

    def current_for_borrower(self) -> bool:
        # A render borrower outlives the workspace owner and has no `confirm` of its own for the
        # stored artifact, so each of its questions keeps the full proof.
        with self._lock:
            return self._borrowers > 0 and self._artifact_current(prove=True)

    def owner_retained(self) -> bool:
        """Late metadata-only guard; artifact/store checks are performed separately."""

        with self._lock:
            return not self._owner_released and not self.lease.released

    def claim(self) -> GeneratedAuthoringVideoSource:
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        return self

    def borrow_for_render(self) -> GeneratedAuthoringVideoBorrower:
        with self._lock:
            if self._owner_released or not self._artifact_current(prove=True):
                raise AuthoringSourceBindingError("source_stale")
            self._borrowers += 1
            return GeneratedAuthoringVideoBorrower(self)

    def release(self) -> None:
        lease: OwnedOutputLease | None = None
        with self._lock:
            if self._owner_released:
                return
            self._owner_released = True
            if self._borrowers == 0 and not self.lease.released:
                lease = self.lease
        if lease is not None:
            try:
                lease.release()
            except MediaProcessError:
                pass

    def _release_borrower(self) -> None:
        lease: OwnedOutputLease | None = None
        with self._lock:
            if self._borrowers <= 0:
                return
            self._borrowers -= 1
            if self._owner_released and self._borrowers == 0 and not self.lease.released:
                lease = self.lease
        if lease is not None:
            try:
                lease.release()
            except MediaProcessError:
                pass


@dataclass(slots=True, repr=False)
class GeneratedAuthoringVideoBorrower:
    _source: GeneratedAuthoringVideoSource | None = field(repr=False)

    def current(self) -> bool:
        source = self._source
        return source is not None and source.current_for_borrower()

    @property
    def path(self) -> Path | None:
        source = self._source
        if source is None or not self.current():
            return None
        return source.lease.path

    def release(self) -> None:
        source = self._source
        if source is None:
            return
        self._source = None
        source._release_borrower()

    def __repr__(self) -> str:
        return "<GeneratedAuthoringVideoBorrower opaque>"


class CompositeAuthoringSourceBindingReceipt(AuthoringSourceBindingReceipt):
    """One bounded receipt delegating original Context and generated sources."""

    __slots__ = ("_base", "_generated", "_owns_base", "_activated")

    def __init__(
        self,
        base: AuthoringSourceBindingReceipt | None,
        generated: dict[str, GeneratedAuthoringVideoSource],
    ) -> None:
        if not generated or any(type(key) is not str for key in generated):
            raise AuthoringSourceBindingError("source_count_exceeded")
        exact = base._exact_registry if base is not None else ReferenceRegistry.empty()
        if exact is None or (base is not None and base.released):
            raise AuthoringSourceBindingError("receipt_released")
        generation = 1 if base is None else base.generation + 1
        super().__init__(exact_registry=exact, generation=generation)
        self._base = base
        self._generated = dict(generated)
        self._owns_base = False
        self._activated = False

    def activate(self) -> None:
        with self._lifecycle_lock:
            if self.released or self._activated:
                raise AuthoringSourceBindingError("receipt_released")
            if self._base is not None and self._base.released:
                raise AuthoringSourceBindingError("receipt_released")
            self._owns_base = True
            self._activated = True

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        source = self._generated.get(source_id)
        if source is not None:
            return (
                RuntimeVideoCapability.AVAILABLE
                if source.current()
                else RuntimeVideoCapability.STALE
            )
        if self._base is None:
            raise AuthoringSourceBindingError("source_not_found")
        return self._base._capability_for(source_id)

    def _claim_source(self, source_id: str) -> object:
        source = self._generated.get(source_id)
        if source is not None:
            return source.claim()
        if self._base is None:
            raise AuthoringSourceBindingError("source_not_found")
        return self._base._claim_source(source_id)

    def _duration_for(self, source_id: str) -> int | None:
        source = self._generated.get(source_id)
        if source is not None:
            return source.duration_milliseconds if source.current() else None
        return None if self._base is None else self._base._duration_for(source_id)

    def _release_sources(self) -> None:
        if self._owns_base and self._base is not None:
            self._base.release()
        self._base = None
        for source in self._generated.values():
            source.release()
        self._generated.clear()


def stage_generated_authoring_source(
    *,
    receipt: SegmentArtifactReceipt,
    store: PrivateSegmentArtifactStore,
    media_adapter: QualifiedAVMediaAdapter,
    scratch_root: Path,
    production_is_current: Callable[[], bool],
    deadline: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> GeneratedAuthoringVideoSource:
    """Copy and probe one exact artifact outside both workspace registry locks."""

    if (
        type(receipt) is not SegmentArtifactReceipt
        or type(store) is not PrivateSegmentArtifactStore
        or type(media_adapter) is not QualifiedAVMediaAdapter
        or not isinstance(scratch_root, Path)
        or not callable(production_is_current)
        or not callable(cancelled)
        or type(deadline) is not float
        or not math.isfinite(deadline)
    ):
        raise AuthoringSourceBindingError("source_unavailable")
    suffix = "." + receipt.format_label
    lease = create_output_lease(scratch_root, suffix=suffix)
    normalized = False
    try:
        if cancelled():
            raise AuthoringSourceBindingError("cancelled")
        if time.monotonic() >= deadline:
            raise AuthoringSourceBindingError("timed_out")
        if not production_is_current():
            raise AuthoringSourceBindingError("source_stale")
        store.stream_artifact_into(
            receipt,
            lease.path,
            # CRITICAL: a bounded import must remain cancellable while copying a large private
            # artifact; checking only Production currentness can run past the route deadline.
            should_cancel=lambda: (
                cancelled() or time.monotonic() >= deadline or not production_is_current()
            ),
        )
        if cancelled():
            raise AuthoringSourceBindingError("cancelled")
        if time.monotonic() >= deadline:
            raise AuthoringSourceBindingError("timed_out")
        cancellation_probe = _ImportCancellationProbe(cancelled)
        try:
            facts = probe_authoring_video_facts(
                lease.path,
                media_adapter,
                deadline,
                cancellation=cancellation_probe,
            )
        except AuthoringVideoFactsError as exc:
            if receipt.format_label != "mp4" or exc.code not in {
                "video_facts_unsupported",
                "video_facts_unavailable",
            }:
                raise
            # IMPORTANT: native ComfyUI MP4 can carry sRGB transfer and B-frame timestamps.
            # Convert its exact verified private copy; never relabel the original receipt's bytes.
            replacement = create_output_lease(scratch_root, suffix=".mp4")
            try:
                media_adapter.normalize_generated_authoring_source(
                    source_path=lease.path,
                    output=replacement,
                    deadline=deadline,
                    cancellation=cancellation_probe,
                )
                facts = probe_authoring_video_facts(
                    replacement.path,
                    media_adapter,
                    deadline,
                    cancellation=cancellation_probe,
                )
                lease.release()
            except BaseException:
                replacement.release()
                raise
            lease = replacement
            normalized = True
        expected_shape = receipt.shape
        if (
            len(expected_shape) != 4
            or expected_shape[3] != 3
            or (facts.frame_count, facts.height, facts.width) != expected_shape[:3]
            or (
                not normalized
                and (
                    facts.content_fingerprint != receipt.output_fingerprint
                    or facts.byte_length != receipt.byte_length
                )
            )
        ):
            # SECURITY: a valid receipt hash identifies bytes, but its declared media contract
            # also gates Authoring. Never relabel mismatched decoded geometry as an admitted asset.
            raise AuthoringSourceBindingError("source_unavailable")
        source = GeneratedAuthoringVideoSource(
            lease=lease,
            facts=facts,
            expected_receipt=receipt,
            store=store,
            production_is_current=production_is_current,
        )
        if not source.current():
            raise AuthoringSourceBindingError("source_stale")
        return source
    except BaseException as exc:
        try:
            lease.release()
        except MediaProcessError:
            pass
        if isinstance(exc, ArtifactStoreError):
            if cancelled():
                raise AuthoringSourceBindingError("cancelled") from None
            if time.monotonic() >= deadline:
                raise AuthoringSourceBindingError("timed_out") from None
            if not production_is_current():
                raise AuthoringSourceBindingError("source_stale") from None
            raise AuthoringSourceBindingError("source_unavailable") from None
        if isinstance(exc, AuthoringVideoFactsError):
            if cancelled():
                raise AuthoringSourceBindingError("cancelled") from None
            if time.monotonic() >= deadline or exc.code in {
                "preview_deadline",
                "preview_timeout",
                "process_timeout",
            }:
                raise AuthoringSourceBindingError("timed_out") from None
            raise AuthoringSourceBindingError(exc.code) from None
        if isinstance(exc, AVMediaAdapterError):
            if cancelled():
                raise AuthoringSourceBindingError("cancelled") from None
            if time.monotonic() >= deadline or exc.code in {
                "preview_deadline",
                "preview_timeout",
                "process_timeout",
            }:
                raise AuthoringSourceBindingError("timed_out") from None
            raise AuthoringSourceBindingError(exc.code) from None
        if isinstance(exc, MediaProcessError):
            code = getattr(exc, "code", "source_unavailable")
            raise AuthoringSourceBindingError(code) from None
        raise


__all__ = [
    "GENERATED_AUTHORING_SOURCE_SCHEMA",
    "CompositeAuthoringSourceBindingReceipt",
    "GeneratedAuthoringVideoBorrower",
    "GeneratedAuthoringVideoSource",
    "stage_generated_authoring_source",
]
