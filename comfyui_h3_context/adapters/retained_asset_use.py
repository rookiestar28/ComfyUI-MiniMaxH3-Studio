"""Fresh process-owned Authoring authority for revalidated retained media only."""

from __future__ import annotations

import hashlib
import threading
import uuid
import weakref
from collections.abc import Callable
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
from .authoring_source_binding import (
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    RuntimeVideoCapability,
)
from .authoring_video_facts import AuthoringVideoFacts
from .durable_state_store import _read
from .media_subprocess import OwnedOutputLease
from .segment_artifact_store import _identity

_TOKEN = object()
_ISSUED: weakref.WeakSet[RetainedVideoSource] = weakref.WeakSet()
_GUARD = threading.RLock()


class RetainedVideoSource:
    """Own the fresh copy; neither catalog bytes nor former Production state own this source."""

    __slots__ = (
        "lease",
        "facts",
        "_current",
        "_finalized",
        "_identity",
        "_released",
        "_borrowers",
        "_lock",
        "__weakref__",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"lease", "facts", "_current", "_finalized", "_identity"} and hasattr(
            self, name
        ):
            raise AttributeError("retained source identity is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        lease: OwnedOutputLease,
        facts: AuthoringVideoFacts,
        current: Callable[[], bool],
        finalized: Callable[[], None],
        *,
        _token: object,
    ) -> None:
        if (
            _token is not _TOKEN
            or type(lease) is not OwnedOutputLease
            or type(facts) is not AuthoringVideoFacts
        ):
            raise AuthoringSourceBindingError("source_unavailable")
        self.lease, self.facts = lease, facts
        self._current, self._finalized = current, finalized
        self._identity = _identity(lease.path.lstat())
        self._released = False
        self._borrowers = 0
        self._lock = threading.RLock()
        with _GUARD:
            _ISSUED.add(self)

    def __repr__(self) -> str:
        return "<RetainedVideoSource opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("retained sources are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("retained sources are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("retained sources are not serializable")

    def _prove(self, *, borrower: bool = False) -> bool:
        with _GUARD:
            if self not in _ISSUED:
                return False
        with self._lock:
            if self.lease.released or (self._borrowers == 0 if borrower else self._released):
                return False
        try:
            # Do not hold this source lock while entering its owner's catalog/lease guard.
            if not self._current():
                return False
            body, metadata = _read(self.lease.path, self.facts.byte_length)
            return (
                not self.lease.released
                and _identity(metadata) == self._identity
                and len(body) == self.facts.byte_length
                and "sha256:" + hashlib.sha256(body).hexdigest() == self.facts.content_fingerprint
            )
        except Exception:
            return False

    def current(self) -> bool:
        return self._prove()

    def verify_artifact(self) -> bool:
        return self._prove()

    @property
    def duration_milliseconds(self) -> int:
        last, base = self.facts.landmarks[-1], self.facts.source_time_base
        return ((last.pts + last.duration_ticks) * base.num * 1000 + base.den - 1) // base.den

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
                "pixel_aspect": None
                if self.facts.pixel_aspect_ratio is None
                else self.facts.pixel_aspect_ratio.to_wire(),
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

    def claim(self) -> RetainedVideoSource:
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        return self

    def borrow_for_render(self) -> RetainedVideoBorrower:
        if not self.current():
            raise AuthoringSourceBindingError("source_stale")
        with self._lock:
            if self._released or self.lease.released:
                raise AuthoringSourceBindingError("source_stale")
            self._borrowers += 1
        return RetainedVideoBorrower(self, _token=_TOKEN)

    def _finish(self) -> None:
        try:
            self.lease.release()
        except MediaProcessError:
            raise AuthoringSourceBindingError("source_release_failed") from None
        self._finalized()

    def release(self) -> None:
        with self._lock:
            self._released = True
            finish = not self.lease.released and not self._borrowers
        if finish:
            self._finish()

    def _release_borrower(self) -> None:
        with self._lock:
            if not self._borrowers:
                return
            self._borrowers -= 1
            finish = self._released and not self._borrowers and not self.lease.released
        if finish:
            self._finish()


class RetainedVideoBorrower:
    __slots__ = ("_source",)

    def __init__(self, source: RetainedVideoSource, *, _token: object) -> None:
        if _token is not _TOKEN:
            raise AuthoringSourceBindingError("source_unavailable")
        self._source: RetainedVideoSource | None = source

    def __copy__(self) -> NoReturn:
        raise TypeError("retained borrowers are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("retained borrowers are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("retained borrowers are not serializable")

    def current(self) -> bool:
        source = self._source
        return source is not None and source._prove(borrower=True)

    @property
    def path(self) -> Path | None:
        source = self._source
        return source.lease.path if source is not None and self.current() else None

    def release(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            source._release_borrower()

    def __repr__(self) -> str:
        return "<RetainedVideoBorrower opaque>"


class _RetainedBindingReceipt(AuthoringSourceBindingReceipt):
    __slots__ = ("_source", "_source_id", "_duration")

    def __init__(self, source: RetainedVideoSource, source_id: str, duration: int) -> None:
        # CRITICAL: every revalidated use starts with a new exact registry identity. A saved
        # fingerprint, former callback or copied ContextPlan must never resurrect a binding.
        super().__init__(exact_registry=ReferenceRegistry.empty(), generation=1)
        self._source, self._source_id, self._duration = source, source_id, duration

    def _capability_for(self, source_id: str) -> RuntimeVideoCapability:
        if source_id != self._source_id:
            raise AuthoringSourceBindingError("source_not_found")
        return (
            RuntimeVideoCapability.AVAILABLE
            if self._source.current()
            else RuntimeVideoCapability.STALE
        )

    def _claim_source(self, source_id: str) -> object:
        if source_id != self._source_id:
            raise AuthoringSourceBindingError("source_not_found")
        return self._source.claim()

    def _duration_for(self, source_id: str) -> int | None:
        return self._duration if source_id == self._source_id and self._source.current() else None

    def _release_sources(self) -> None:
        self._source.release()


class RetainedAssetUse:
    """Backend-only fresh source port; its public projection contains no source locator."""

    __slots__ = ("asset_id", "use_handle", "source_id", "receipt", "source", "expires_at")

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("retained use identity is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        asset_id: str,
        source: RetainedVideoSource,
        duration: int,
        expires_at: float,
        *,
        _token: object,
    ) -> None:
        if _token is not _TOKEN:
            raise AuthoringSourceBindingError("source_unavailable")
        self.asset_id, self.source = asset_id, source
        self.use_handle, self.source_id = (
            "retained_" + uuid.uuid4().hex,
            "retained." + uuid.uuid4().hex,
        )
        self.receipt = _RetainedBindingReceipt(source, self.source_id, duration)
        self.expires_at = expires_at

    def release(self) -> None:
        self.receipt.release()
        # IMPORTANT: receipt revocation is final even if owned-file cleanup failed. Retry the
        # resource owner separately or a later release would leave media protected forever.
        self.source.release()

    def __repr__(self) -> str:
        return "<RetainedAssetUse opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("retained uses are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("retained uses are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("retained uses are not serializable")


def _mint_retained_use(
    *,
    asset_id: str,
    lease: OwnedOutputLease,
    facts: AuthoringVideoFacts,
    duration: int,
    expires_at: float,
    current: Callable[[], bool],
    finalized: Callable[[], None],
) -> RetainedAssetUse:
    source = RetainedVideoSource(lease, facts, current, finalized, _token=_TOKEN)
    return RetainedAssetUse(asset_id, source, duration, expires_at, _token=_TOKEN)
