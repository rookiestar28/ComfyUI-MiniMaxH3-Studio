"""M20-08 focused tests for the incremental segment transport.

The transport must stream an exact reusable stored artifact into a caller-owned fresh file one
bounded chunk at a time, verify content identity en route, fail closed on tamper, cancellation,
clobber and identity divergence, and never expose a store locator. No fixture carries a prompt,
media value, URL or credential; payloads are synthetic bytes.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_segment_artifact_store import _partial, _policy

from comfyui_h3_context.adapters import segment_artifact_store as store_module
from comfyui_h3_context.adapters.av_reconstruction_transport import (
    ArtifactStoreSegmentSource,
    AVSegmentSource,
    AVTransportError,
)
from comfyui_h3_context.adapters.segment_artifact_store import (
    ArtifactStoreError,
    PrivateSegmentArtifactStore,
)
from comfyui_h3_context.core.segment_artifacts import SegmentArtifactReceipt


def _fp(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _completed_store(
    root: Path, payload: bytes
) -> tuple[PrivateSegmentArtifactStore, SegmentArtifactReceipt]:
    store = PrivateSegmentArtifactStore(
        root / "private-store",
        policy=_policy(max_artifact_bytes=4096, max_total_bytes=8192),
        clock_ms=lambda: 100,
    )
    completed = store.commit(store.begin(_partial()), payload)
    return store, completed


class _Cancellation:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.cancelled


def test_source_streams_exact_bytes_one_bounded_chunk_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"synthetic-artifact-body-" * 32
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        assert isinstance(source, AVSegmentSource)
        assert source.byte_length == len(payload)
        assert source.content_fingerprint == _fp(payload)
        # A tiny chunk proves the copy is genuinely incremental, not one whole read.
        monkeypatch.setattr(store_module, "_STREAM_CHUNK_BYTES", 7)
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        cancellation = _Cancellation()
        length, fingerprint = source.stream_into(destination, cancellation=cancellation)
        assert (length, fingerprint) == (len(payload), _fp(payload))
        assert destination.read_bytes() == payload
        # One cancellation poll per chunk: the stream really was chunked.
        assert cancellation.calls >= len(payload) // 7


def test_destination_is_never_clobbered() -> None:
    payload = b"synthetic-artifact-body"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        destination.write_bytes(b"pre-existing")
        with pytest.raises(AVTransportError) as caught:
            source.stream_into(destination)
        assert caught.value.code == "transport_unavailable"
        assert destination.read_bytes() == b"pre-existing"


def test_cancellation_stops_the_stream_and_removes_the_partial_copy() -> None:
    payload = b"synthetic-artifact-body-" * 8
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        with pytest.raises(AVTransportError) as caught:
            source.stream_into(destination, cancellation=_Cancellation(cancelled=True))
        assert caught.value.code == "transport_cancelled"
        assert not destination.exists()


def test_a_broken_cancellation_probe_stops_the_stream() -> None:
    payload = b"synthetic-artifact-body"

    class _Broken:
        def is_cancelled(self) -> bool:
            raise RuntimeError("probe failure")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        with pytest.raises(AVTransportError) as caught:
            source.stream_into(destination, cancellation=_Broken())
        assert caught.value.code == "transport_cancelled"
        assert not destination.exists()


def test_a_tampered_artifact_never_streams() -> None:
    payload = b"synthetic-artifact-body"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        artifact_path = root / "private-store" / "artifacts" / "artifact.1.bin"
        artifact_path.write_bytes(b"tampered-artifact-body!")
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        with pytest.raises(AVTransportError) as caught:
            source.stream_into(destination)
        assert caught.value.code == "transport_unavailable"
        assert not destination.exists()


def test_an_expired_or_incompatible_receipt_never_streams() -> None:
    payload = b"synthetic-artifact-body"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        incompatible = replace(
            completed,
            model_fingerprint="sha256:" + "d" * 64,
            receipt_fingerprint=None,
        )
        source = ArtifactStoreSegmentSource(store=store, receipt=incompatible)
        destination = root / "staging" / "segment.media"
        destination.parent.mkdir()
        with pytest.raises(AVTransportError) as caught:
            source.stream_into(destination)
        assert caught.value.code == "transport_unavailable"
        assert not destination.exists()


def test_transport_configuration_is_closed() -> None:
    payload = b"synthetic-artifact-body"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        # A PARTIAL receipt is a valid receipt with no content identity to declare.
        with pytest.raises(AVTransportError) as caught:
            ArtifactStoreSegmentSource(store=store, receipt=_partial())
        assert caught.value.code == "transport_configuration"
        with pytest.raises(AVTransportError) as caught:
            ArtifactStoreSegmentSource(
                store=object(),  # type: ignore[arg-type]
                receipt=completed,
            )
        assert caught.value.code == "transport_configuration"


def test_no_store_locator_reaches_the_public_surface() -> None:
    payload = b"synthetic-artifact-body"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store, completed = _completed_store(root, payload)
        source = ArtifactStoreSegmentSource(store=store, receipt=completed)
        public = f"{source.byte_length} {source.content_fingerprint}"
        try:
            destination = root / "missing-parent" / "segment.media"
            source.stream_into(destination)
        except AVTransportError as exc:
            public += f" {exc.code} {exc}"
            # One level of the cause chain: the typed store error a caller could log.
            cause = exc.__cause__
            if isinstance(cause, ArtifactStoreError):
                public += f" {cause}"
        lowered = public.lower()
        for forbidden in ("\\\\", "private-store", "artifacts", "://"):
            assert forbidden not in lowered
