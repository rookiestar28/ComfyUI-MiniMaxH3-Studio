"""M20-08: owned incremental segment transport over exact accepted artifacts.

The reconstruction executor used to require every admitted input as one complete in-memory
``bytes`` payload. This module is the bounded alternative: an ``AVSegmentSource`` declares the
exact content identity of one accepted segment artifact (byte length plus sha256 fingerprint)
and can stream that content into an adapter-owned staging file one bounded chunk at a time.
Verification is content-addressed — the stream is hashed as it is copied and any divergence
from the declared identity fails closed before the copy is used. Callers never receive a
filesystem locator: the one production source is backed by ``PrivateSegmentArtifactStore``, which
keeps its member paths private and streams through its own pinned-read primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.segment_artifacts import SegmentArtifactReceipt
from .media_subprocess import CancellationProbe
from .segment_artifact_store import ArtifactStoreError, PrivateSegmentArtifactStore


class AVTransportError(Exception):
    """One typed transport outcome with a content-free machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@runtime_checkable
class AVSegmentSource(Protocol):
    """One re-streamable, content-addressed segment input.

    ``stream_into`` may be called more than once (the rolling assembly re-streams a
    non-passthrough segment for its transform after the verification pass released the first
    staged copy); every call produces a complete fresh copy verified against the declared
    identity.
    """

    @property
    def byte_length(self) -> int: ...

    @property
    def content_fingerprint(self) -> str: ...

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[int, str]: ...


def _should_cancel(cancellation: CancellationProbe | None) -> bool:
    if cancellation is None:
        return False
    try:
        cancelled = cancellation.is_cancelled()
    except Exception:  # noqa: BLE001 - a broken probe must stop the stream, not leak.
        return True
    return type(cancelled) is not bool or cancelled


@dataclass(frozen=True, slots=True)
class ArtifactStoreSegmentSource:
    """The production ``AVSegmentSource``: one exact reusable stored segment artifact."""

    store: PrivateSegmentArtifactStore
    receipt: SegmentArtifactReceipt

    def __post_init__(self) -> None:
        if (
            type(self.store) is not PrivateSegmentArtifactStore
            or type(self.receipt) is not SegmentArtifactReceipt
        ):
            raise AVTransportError("transport_configuration")
        if (
            type(self.receipt.byte_length) is not int
            or self.receipt.byte_length <= 0
            or type(self.receipt.output_fingerprint) is not str
        ):
            raise AVTransportError("transport_configuration")

    @property
    def byte_length(self) -> int:
        return self.receipt.byte_length

    @property
    def content_fingerprint(self) -> str:
        fingerprint = self.receipt.output_fingerprint
        if fingerprint is None:  # pragma: no cover - excluded by __post_init__
            raise AVTransportError("transport_configuration")
        return fingerprint

    def stream_into(
        self,
        destination: Path,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[int, str]:
        try:
            length, fingerprint = self.store.stream_artifact_into(
                self.receipt,
                destination,
                should_cancel=lambda: _should_cancel(cancellation),
            )
        except ArtifactStoreError as exc:
            if exc.code == "stream_cancelled":
                raise AVTransportError("transport_cancelled") from exc
            raise AVTransportError("transport_unavailable") from exc
        if length != self.byte_length or fingerprint != self.content_fingerprint:
            raise AVTransportError("transport_identity_mismatch")
        return length, fingerprint


__all__ = [
    "ArtifactStoreSegmentSource",
    "AVSegmentSource",
    "AVTransportError",
]
