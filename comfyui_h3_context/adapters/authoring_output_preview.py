"""Explicit pinned final-preview derivation, separate from original render receipts."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from ..core.authoring_output_protocol import (
    OUTPUT_MAX_BYTES,
    OUTPUT_MAX_FRAMES,
    PREVIEW_MAX_BYTES,
    OutputProtocolError,
)
from ..core.authoring_render_jobs import RenderJobLimits
from ..core.authoring_render_receipts import MeasuredRenderOutput, render_video_timing_fingerprint
from .authoring_render_probe import measure_render_output
from .authoring_render_process import WindowsRenderProcessSession, pin_render_executable
from .authoring_render_store import RenderOutputStore
from .authoring_renderer_qualification import load_renderer_qualification
from .segment_artifact_store import (
    _read_regular_bytes,
    _validated_directories,
    _windows_open_existing_file,
)

_LIMITS = RenderJobLimits(
    deadline_ms=60_000,
    child_memory_bytes=256 * 1024 * 1024,
    child_cpu_seconds=60,
    max_staging_bytes=528 * 1024 * 1024,
    max_output_bytes=PREVIEW_MAX_BYTES,
    retained_output_bytes=256 * 1024 * 1024,
)


def preview_dimensions(width: int, height: int) -> tuple[int, int]:
    if (
        type(width) is not int
        or type(height) is not int
        or not 2 <= width <= 1920
        or not 2 <= height <= 1080
        or width % 2
        or height % 2
    ):
        raise OutputProtocolError("preview_unavailable")
    ratio = min(Fraction(1), Fraction(640, width), Fraction(360, height))
    return max(2, int(width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


def validate_preview(
    body: bytes, facts: MeasuredRenderOutput, parent: MeasuredRenderOutput
) -> None:
    if (
        type(body) is not bytes
        or not 1 <= len(body) <= PREVIEW_MAX_BYTES
        or type(facts) is not MeasuredRenderOutput
        or type(parent) is not MeasuredRenderOutput
        or not 1 <= parent.frame_count <= OUTPUT_MAX_FRAMES
        or parent.audio_streams not in (0, 1)
    ):
        raise OutputProtocolError("preview_unavailable")
    width, height = preview_dimensions(parent.width, parent.height)
    fixed = {
        "container": "mp4",
        "video_codec": "h264",
        "video_streams": 1,
        "other_streams": 0,
        "frame_rate_num": 24,
        "frame_rate_den": 1,
        "time_base_num": 1,
        "time_base_den": 24,
        "pixel_format": "yuv420p",
        "pixel_aspect_num": 1,
        "pixel_aspect_den": 1,
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "audio_codec": "aac" if parent.audio_streams else None,
        "audio_sample_rate": 48000 if parent.audio_streams else None,
        "audio_channels": 1 if parent.audio_streams else None,
        "audio_effective_samples": parent.frame_count * 2000 if parent.audio_streams else None,
        "video_timing_fingerprint": render_video_timing_fingerprint(
            tuple(range(parent.frame_count)),
            tuple(range(parent.frame_count)),
            (1,) * parent.frame_count,
        ),
    }
    if any(getattr(parent, key) != value for key, value in fixed.items()):
        raise OutputProtocolError("preview_unavailable")
    # IMPORTANT: only dimensions and encoded bytes may differ. Audio stream-copy must retain
    # every effective sample and complete video timing; a playable first frame is insufficient.
    expected = replace(
        parent,
        width=width,
        height=height,
        byte_length=len(body),
        output_fingerprint="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    if facts != expected:
        raise OutputProtocolError("preview_unavailable")


class _Control:
    def __init__(self, stopped: threading.Event, check: Callable[[], None]) -> None:
        self.deadline = time.monotonic() + 60.0
        self._stopped = stopped
        self._check = check

    def is_cancelled(self) -> bool:
        if self._stopped.is_set():
            return True
        try:
            self._check()
        except Exception:
            return True
        return False


class NativeOutputPreview:
    """Own one guarded staging session; no original artifact path is ever consumed."""

    def __init__(self, *, root: Path, renderer_path: Path, probe_path: Path) -> None:
        if sys.platform != "win32" or not all(
            isinstance(path, Path) for path in (root, renderer_path, probe_path)
        ):
            raise OutputProtocolError("preview_unavailable")
        self._renderer_path = renderer_path
        self._probe_path = probe_path
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._store = RenderOutputStore(root, limits=_LIMITS)

    def __repr__(self) -> str:
        return "<NativeOutputPreview opaque>"

    def render(
        self, body: bytes, parent: MeasuredRenderOutput, check: Callable[[], None]
    ) -> tuple[bytes, MeasuredRenderOutput]:
        if (
            self._stop.is_set()
            or type(body) is not bytes
            or not 1 <= len(body) <= OUTPUT_MAX_BYTES
            or type(parent) is not MeasuredRenderOutput
            or len(body) != parent.byte_length
            or "sha256:" + hashlib.sha256(body).hexdigest() != parent.output_fingerprint
        ):
            raise OutputProtocolError("preview_unavailable")
        if not self._lock.acquire(blocking=False):
            raise OutputProtocolError("resource_limit")
        stage = None
        try:
            check()
            if self._stop.is_set():
                raise OutputProtocolError("preview_unavailable")
            width, height = preview_dimensions(parent.width, parent.height)
            identity = load_renderer_qualification()
            control = _Control(self._stop, check)
            stage = self._store.begin("render-" + uuid.uuid4().hex, parent.output_fingerprint)
            source = stage.write_input(body, suffix=".mp4")
            with ExitStack() as scope:
                scope.enter_context(_validated_directories(source.parent))
                # CRITICAL: deny source write/delete through encode and all independent probes.
                # The copied input must remain the exact verified parent, not a reopened pathname.
                descriptor = _windows_open_existing_file(source)
                scope.callback(os.close, descriptor)
                digest = hashlib.sha256()
                total = 0
                while block := os.read(descriptor, 1024 * 1024):
                    check()
                    total += len(block)
                    if total > OUTPUT_MAX_BYTES:
                        raise OutputProtocolError("preview_unavailable")
                    digest.update(block)
                if (
                    total != len(body)
                    or "sha256:" + digest.hexdigest() != parent.output_fingerprint
                ):
                    raise OutputProtocolError("preview_unavailable")
                renderer = scope.enter_context(
                    pin_render_executable(
                        self._renderer_path, identity.renderer_fingerprint, control=control
                    )
                )
                probe = scope.enter_context(
                    pin_render_executable(
                        self._probe_path, identity.probe_fingerprint, control=control
                    )
                )
                session = scope.enter_context(WindowsRenderProcessSession(limits=_LIMITS))
                arguments = (
                    "-nostdin",
                    "-v",
                    "error",
                    "-n",
                    "-threads",
                    "2",
                    "-filter_threads",
                    "2",
                    "-filter_complex_threads",
                    "2",
                    "-max_alloc",
                    "67108864",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    "mov",
                    "-enable_drefs",
                    "0",
                    "-use_absolute_path",
                    "0",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-vf",
                    f"scale={width}:{height}:flags=lanczos+accurate_rnd,format=yuv420p,setsar=1",
                    "-c:v",
                    "libx264",
                    "-threads:v",
                    "2",
                    "-bf",
                    "0",
                    "-b:v",
                    "480k",
                    "-maxrate",
                    "480k",
                    "-bufsize",
                    "960k",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_range",
                    "tv",
                    "-colorspace",
                    "bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-video_track_timescale",
                    "24",
                    "-movie_timescale",
                    "48000",
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-movflags",
                    "+faststart",
                    *(("-map", "0:a:0", "-c:a", "copy") if parent.audio_streams == 1 else ("-an",)),
                    "-f",
                    "mp4",
                    str(stage.output_path),
                )
                result = session.run(
                    renderer,
                    arguments,
                    cwd=source.parent,
                    control=control,
                    maximum_stdout=65536,
                    check_staging=stage.check_budget,
                )
                if (
                    result.exit_code
                    or result.diagnostic_bytes
                    or result.active_processes
                    or not result.limits_verified
                ):
                    raise OutputProtocolError("preview_unavailable")
                facts = measure_render_output(
                    path=stage.output_path,
                    probe=probe,
                    session=session,
                    control=control,
                    limits=_LIMITS,
                    check_staging=stage.check_budget,
                )
                preview = _read_regular_bytes(stage.output_path, maximum_bytes=PREVIEW_MAX_BYTES)
                validate_preview(preview, facts, parent)
                check()
                if control.is_cancelled() or time.monotonic() >= control.deadline:
                    raise OutputProtocolError("preview_unavailable")
                return preview, facts
        except OutputProtocolError:
            raise
        except Exception:
            raise OutputProtocolError("preview_unavailable") from None
        finally:
            try:
                if stage is not None:
                    self._store.discard(stage)
            finally:
                self._lock.release()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._store.close()
