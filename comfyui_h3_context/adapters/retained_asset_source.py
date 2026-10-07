"""Factory-owned live Production byte leases for optional retained-media copies."""

from __future__ import annotations

import math
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, SupportsIndex

from ..core.durable_workspace_state import DurableStateError, require_owner
from ..core.retained_assets import RetainedAssetError
from .authoring_generated_source import (
    GeneratedAuthoringVideoBorrower,
    GeneratedAuthoringVideoSource,
    stage_generated_authoring_source,
)
from .authoring_source_binding import AuthoringSourceBindingError
from .authoring_video_facts import AuthoringVideoFacts
from .av_reconstruction_media import QualifiedAVMediaAdapter
from .comfyui_production_workspace import ProductionAuthoringOutputClaim
from .segment_artifact_store import ArtifactStoreError, _copy_regular_file_into_new

_FACTORY_TOKEN = object()
_ISSUED: weakref.WeakSet[VerifiedRetentionSource] = weakref.WeakSet()
_ISSUANCE_LOCK = threading.RLock()


def _owner(value: object) -> str:
    try:
        return require_owner(value)
    except DurableStateError as error:
        raise RetainedAssetError(str(error)) from None


def check_retention_budget(deadline: float, cancelled: Callable[[], bool]) -> None:
    if type(deadline) is not float or not math.isfinite(deadline) or not callable(cancelled):
        raise RetainedAssetError("source_unavailable")
    result = cancelled()
    if type(result) is not bool:
        raise RetainedAssetError("source_unavailable")
    if result:
        raise RetainedAssetError("cancelled")
    if time.monotonic() >= deadline:
        raise RetainedAssetError("timed_out")


class VerifiedRetentionSource:
    """One non-copyable source issued only after exact current Production admission."""

    __slots__ = ("_owner_id", "_source", "_borrower", "_facts", "_duration", "_lock", "__weakref__")

    def __init__(
        self,
        *,
        owner_id: str,
        source: GeneratedAuthoringVideoSource,
        borrower: GeneratedAuthoringVideoBorrower,
        _token: object,
    ) -> None:
        if _token is not _FACTORY_TOKEN:
            raise RetainedAssetError("source_unavailable")
        self._owner_id = _owner(owner_id)
        self._source: GeneratedAuthoringVideoSource | None = source
        self._borrower: GeneratedAuthoringVideoBorrower | None = borrower
        self._facts = source.facts
        self._duration = source.duration_milliseconds
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "<VerifiedRetentionSource opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("retention sources are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("retention sources are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("retention sources are not serializable")

    @property
    def facts(self) -> AuthoringVideoFacts:
        return self._facts

    @property
    def duration_milliseconds(self) -> int:
        return self._duration

    def current(self) -> bool:
        with self._lock:
            borrower = self._borrower
            return self._source is not None and borrower is not None and borrower.current()

    def copy_into(
        self,
        destination: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> tuple[int, str]:
        """Stream a fresh exclusive copy; a failed copy is never a retained acknowledgement."""
        check_retention_budget(deadline, cancelled)
        with self._lock:
            if not self.current() or self._borrower is None:
                raise RetainedAssetError("source_stale")
            path = self._borrower.path
            if path is None:
                raise RetainedAssetError("source_stale")
            try:
                size, digest = _copy_regular_file_into_new(
                    path,
                    destination,
                    maximum_bytes=self.facts.byte_length,
                    should_cancel=lambda: cancelled() or time.monotonic() >= deadline,
                )
            except (ArtifactStoreError, OSError):
                check_retention_budget(deadline, cancelled)
                if not self.current():
                    raise RetainedAssetError("source_stale") from None
                raise RetainedAssetError("source_unavailable") from None
            # CRITICAL: normalized admitted bytes have their own facts. Neither an old raw-output
            # receipt nor a same-size leased-file replacement may supply the copied media digest.
            if (size, digest) != (self.facts.byte_length, self.facts.content_fingerprint):
                raise RetainedAssetError("asset_changed")
            check_retention_budget(deadline, cancelled)
            if not self.current():
                raise RetainedAssetError("source_stale")
            return size, digest

    def release(self) -> None:
        with self._lock:
            borrower, source = self._borrower, self._source
            self._borrower = self._source = None
        if borrower is not None:
            borrower.release()
        if source is not None:
            source.release()


def require_retention_source(value: object, *, owner_id: str) -> VerifiedRetentionSource:
    owner = _owner(owner_id)
    # SECURITY: type/facts alone are forgeable Python values. Only this factory's live issued
    # identity can be used by the writer; decoded metadata never enters the issuance set.
    with _ISSUANCE_LOCK:
        if type(value) is not VerifiedRetentionSource or value not in _ISSUED:
            raise RetainedAssetError("source_unavailable")
    if value._owner_id != owner:
        raise RetainedAssetError("owner_mismatch")
    if not value.current():
        raise RetainedAssetError("source_stale")
    return value


def stage_retention_source(
    *,
    claim: ProductionAuthoringOutputClaim,
    owner_id: str,
    media_adapter: QualifiedAVMediaAdapter,
    scratch_root: Path,
    deadline: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> VerifiedRetentionSource:
    owner = _owner(owner_id)
    check_retention_budget(deadline, cancelled)
    if (
        type(claim) is not ProductionAuthoringOutputClaim
        or type(media_adapter) is not QualifiedAVMediaAdapter
    ):
        raise RetainedAssetError("source_unavailable")
    if not claim.current():
        raise RetainedAssetError("source_stale")
    source: GeneratedAuthoringVideoSource | None = None
    borrower: GeneratedAuthoringVideoBorrower | None = None
    try:
        source = stage_generated_authoring_source(
            receipt=claim.receipt,
            store=claim.store,
            media_adapter=media_adapter,
            scratch_root=scratch_root,
            production_is_current=claim.current,
            deadline=deadline,
            cancelled=cancelled,
        )
        borrower = source.borrow_for_render()
        result = VerifiedRetentionSource(
            owner_id=owner, source=source, borrower=borrower, _token=_FACTORY_TOKEN
        )
        check_retention_budget(deadline, cancelled)
        if not result.current():
            raise RetainedAssetError("source_stale")
        with _ISSUANCE_LOCK:
            _ISSUED.add(result)
        return result
    except BaseException as error:
        if borrower is not None:
            borrower.release()
        if source is not None:
            source.release()
        if isinstance(error, AuthoringSourceBindingError):
            code = (
                error.code
                if error.code in {"source_stale", "cancelled", "timed_out"}
                else "media_unqualified"
            )
            raise RetainedAssetError(code) from None
        raise
