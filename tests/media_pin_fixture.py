"""Portable media-content double for tests that already replace native processes."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from comfyui_h3_context.adapters.av_reconstruction_media import _read_regular_media_body
from comfyui_h3_context.adapters.media_subprocess import CancellationProbe


@contextmanager
def semantic_media_pin(
    path: Path,
    maximum_bytes: int,
    *,
    deadline: float | None = None,
    cancellation: CancellationProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[tuple[int, str]]:
    # IMPORTANT: this double preserves content/budget checks, not OS pinning or sharing safety.
    # Native identity and write-denial tests must keep the real pin and their Windows boundary.
    body, fingerprint = _read_regular_media_body(
        path,
        maximum_bytes,
        deadline=float("inf") if deadline is None else deadline,
        cancellation=cancellation,
        clock=clock,
    )
    size = len(body)
    del body
    yield size, fingerprint
