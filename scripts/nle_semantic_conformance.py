"""M25-20: the bounded offline extraction and qualification entry point.

This is the tooling half of the independent final observation. It constructs its own decoder command
lines, streams the result past a bounded buffer, hands the bytes to the pure extractor in
``comfyui_h3_context.core.semantic_conformance_extract``, and writes a closed report. It never calls
the render planner, the render graph or a render receipt to obtain an observed value.

Three rules are enforced here rather than trusted:

- **The decoder is the pinned one or there is no run.** Both binaries are located by explicit
  configuration and verified by digest against the packaged renderer qualification. A binary found
  on ``PATH`` is refused, because "some ffmpeg" measuring "some artifact" is not a qualification.
- **Buffers stay small.** Video is consumed one frame at a time and audio one second at a time. The
  extractor never receives, and this script never holds, a whole decoded video.
- **A timeout is missing evidence.** Every bound in this file turns into ``BLOCKED``, never into a
  relaxed comparison or a shorter corpus.

Scope of ``--artifact`` today: the facts that are properties of the artifact itself -- container,
codec, pixel format, dimensions, pixel aspect, frame rate, audio stream shape, the frame grid built
from frames actually decoded rather than from the container's ``nb_frames`` hint, the measured
packet timing, and the decoded audio extent. The per-case landmarks -- patch means, fiducial
geometry, frame-identifier tiles and impulse onsets -- need the fixture's own descriptor saying
where to look, so they arrive with the corpus fixtures rather than being guessed here.
``--describe`` prints the resolved pins and corpus totals without decoding anything.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import queue
import subprocess  # noqa: S404 -- decode-only invocations of the pinned, digest-verified binaries
import sys
import threading
import time
from collections.abc import Generator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    CONFORMANCE_REPORT_SCHEMA,
    CORPUS_VERSION,
    MAX_CASE_PAYLOAD_BYTES,
    MAX_REPORT_BYTES,
    TOLERANCE_PROFILE,
    TOLERANCE_VERSION,
    SemanticConformanceError,
    admit_report,
    build_corpus,
)
from comfyui_h3_context.core.semantic_conformance_compare import (  # noqa: E402
    OUTPUT_SAMPLE_RATE,
    CaseOutcome,
    summarize,
)
from comfyui_h3_context.core.semantic_conformance_expect import (  # noqa: E402
    GeometryTarget,
    IdentityRead,
    SamplePoint,
)
from comfyui_h3_context.core.semantic_conformance_extract import (  # noqa: E402
    PCM16_BYTES_PER_SAMPLE,
    PCM16_FULL_SCALE,
    RGB24_BYTES_PER_PIXEL,
    parse_frame_grid,
    parse_frame_timing,
    parse_output_metadata,
    patch_mean,
    pcm16_peak,
    pcm16_rms,
)
from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    AUDIO_TONE_PEAK,
    FRAME_ID_BITS,
    FRAME_ID_ONE_RGB,
    FRAME_ID_ZERO_RGB,
    GEOMETRY_LUMA_THRESHOLD,
    SOURCE_PROFILES,
    SourceProfile,
    profile_for,
)

QUALIFICATION_PATH = (
    ROOT / "comfyui_h3_context" / "contracts" / "authoring_renderer_qualification_v1.json"
)

#: Per the plan's resource boundary: decode or probe of one artifact may take no longer than this.
DECODE_TIMEOUT_SECONDS = 120
#: Audio is consumed in one-second blocks so the observation buffer stays bounded.
AUDIO_BLOCK_SAMPLES = OUTPUT_SAMPLE_RATE


class QualificationError(RuntimeError):
    """The run cannot proceed with the authority or runtime it was given."""


def _digest(path: Path) -> str:
    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            reader.update(block)
    return reader.hexdigest()


def _packaged_qualification() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(QUALIFICATION_PATH.read_text(encoding="utf-8")))


def _expected_digest(section: str) -> str:
    # `renderer` and `probe` are plain `sha256:` strings in the packaged contract, not objects.
    value = _packaged_qualification().get(section)
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise QualificationError(f"the packaged qualification has no {section} fingerprint")
    return value.removeprefix("sha256:")


def resolve_binary(configured: Path | None, section: str) -> Path:
    """Return the pinned binary, or refuse.

    CRITICAL: there is deliberately no `PATH` lookup and no download. The plan pins both binaries by
    digest, and a conformance measurement taken with a different build is not the measurement the
    pins describe -- it is a different experiment reported under the accepted one's name.
    """

    if configured is None:
        raise QualificationError(
            f"the {section} binary must be configured explicitly; PATH discovery is refused"
        )
    if not configured.is_file():
        raise QualificationError(f"the configured {section} binary does not exist")
    expected = _expected_digest(section)
    actual = _digest(configured)
    if actual != expected:
        raise QualificationError(
            f"the configured {section} binary does not match the accepted qualification digest"
        )
    return configured


def probe_command(ffprobe: Path, artifact: Path) -> tuple[str, ...]:
    """The probe invocation. `-v error` keeps banners out of the parsed document."""

    return (
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(artifact),
    )


def timing_probe_command(ffprobe: Path, artifact: Path) -> tuple[str, ...]:
    """Ask for the video stream's packet timestamps and its time base, and nothing else.

    `-select_streams v:0` and an explicit `-show_entries` list keep this bounded: the audio packet
    table is not collected, and no per-packet field beyond the three timing facts is read. There is
    deliberately no decode here -- a decoded timestamp would be a function of this script rather
    than of the artifact.
    """

    return (
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts,dts,duration:stream=time_base,codec_type",
        str(artifact),
    )


def decode_video_command(ffmpeg: Path, artifact: Path) -> tuple[str, ...]:
    """Decode video to raw rgb24 on stdout. No filter, no scaling, no re-encode."""

    return (
        str(ffmpeg),
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(artifact),
        "-map",
        "0:v:0",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    )


def decode_audio_command(ffmpeg: Path, artifact: Path) -> tuple[str, ...]:
    """Decode audio to signed 16-bit little-endian mono PCM at the accepted output rate.

    No resampling target other than the profile's own rate is offered: resampling an observation to
    make it line up with an expectation is exactly the move the plan forbids.
    """

    return (
        str(ffmpeg),
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(artifact),
        "-map",
        "0:a:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(OUTPUT_SAMPLE_RATE),
        "-",
    )


def iter_blocks(stream: Any, block_bytes: int, *, unit_bytes: int | None = None) -> Iterator[bytes]:
    """Yield blocks, refusing any that ends mid-unit.

    `unit_bytes` is the indivisible thing the block is made of; it defaults to the block itself.

    CRITICAL: video and audio need different answers here and it is tempting to give them one. A
    video block *is* one frame, so a short block is a corrupt frame and must be refused. An audio
    block is one second of samples, and the last one is almost always short -- a nominally
    two-second AAC stream decodes to 96,256 samples, not 96,000, because of decoder priming.
    Refusing that tail reports every real artifact as `BLOCKED` and observes nothing, which is how
    this was found. Admitting a short final block that is *not* a whole number of units would be the
    opposite mistake: padding it would manufacture an observation.
    """

    if block_bytes <= 0:
        raise QualificationError("a decode block must be a positive number of bytes")
    unit = block_bytes if unit_bytes is None else unit_bytes
    if unit <= 0 or block_bytes % unit:
        raise QualificationError("a decode block must be a whole number of units")
    while True:
        block = stream.read(block_bytes)
        if not block:
            return
        if len(block) % unit:
            raise QualificationError("the decoder produced a partial block")
        yield block
        if len(block) != block_bytes:
            # A short but whole-unit block is the end of the stream, never a hiccup mid-stream.
            return


def run_probe(
    command: tuple[str, ...], *, deadline_seconds: float = DECODE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Run one probe invocation under the per-artifact deadline and parse its JSON document.

    `subprocess.run` with a timeout kills and reaps the child itself, so a probe needs no separate
    reader thread; the deadline is still passed explicitly rather than assumed, because the caller
    is spending one budget across several invocations.
    """

    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, digest-verified binary, no shell
            command,
            capture_output=True,
            timeout=deadline_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise QualificationError("the probe exceeded its per-artifact deadline") from None
    if completed.returncode != 0:
        # The decoder's stderr can name a path, so only the exit status is reported.
        raise QualificationError(f"the probe failed with exit status {completed.returncode}")
    return cast(dict[str, Any], json.loads(completed.stdout.decode("utf-8")))


def video_frame_bytes(width: int, height: int) -> int:
    return width * height * RGB24_BYTES_PER_PIXEL


def audio_block_bytes() -> int:
    return AUDIO_BLOCK_SAMPLES * PCM16_BYTES_PER_SAMPLE


#: Sentinel the reader thread puts on the queue when the decoder's stdout reaches end of file.
_DONE = object()
#: How long each teardown step may take once the deadline has already been exceeded.
_TEARDOWN_GRACE_SECONDS = 5
#: How long a bounded `put` or `join` waits before rechecking the stop flag. Small enough that
#: teardown is prompt, large enough that it is not a spin.
_TEARDOWN_POLL_SECONDS = 0.05
#: Windows: the access right needed to ask whether a process object is still running, and the exit
#: code the kernel reports for one that is.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def process_is_running(pid: int) -> bool:
    """Ask the operating system whether a process is still running.

    `Popen.poll` answers from a cached return code, so it says "gone" for a child this script
    already reaped and nothing at all about one it lost track of. Teardown is asserted against the
    kernel instead.
    """

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover -- alive and owned by someone else
            return True
        return True
    # IMPORTANT: native access stays after the POSIX return; Linux stubs omit windll.
    kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):  # pragma: no cover
            return False
        return bool(code.value == _STILL_ACTIVE)
    finally:
        kernel32.CloseHandle(handle)


def _offer(sink: queue.Queue[Any], item: Any, stop: threading.Event) -> bool:
    """Hand one item to the consumer, giving up if teardown has begun.

    CRITICAL: never `put` without a timeout. The queue holds one item, so a consumer that stops
    early -- a `close()` on the generator, an exception in the caller's loop, a deadline -- leaves
    the producer blocked in `put` forever, and closing the child's stdout cannot release a thread
    that is not blocked in `read`. Draining the queue once releases it exactly once, and it then
    blocks again on the next block. The stop flag, not the queue, is what ends this loop.
    """

    while not stop.is_set():
        try:
            sink.put(item, timeout=_TEARDOWN_POLL_SECONDS)
        except queue.Full:
            continue
        return True
    return False


def _pump_blocks(
    stream: Any,
    block_bytes: int,
    unit_bytes: int | None,
    sink: queue.Queue[Any],
    stop: threading.Event,
) -> None:
    """Read the decoder's stdout on its own thread and hand whole blocks to the consumer."""

    try:
        for block in iter_blocks(stream, block_bytes, unit_bytes=unit_bytes):
            if not _offer(sink, block, stop):
                return
        _offer(sink, _DONE, stop)
    except BaseException as exc:  # noqa: BLE001 -- re-raised on the consumer's thread
        _offer(sink, exc, stop)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.kill()
    try:
        process.wait(timeout=_TEARDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover -- a kill the OS did not honour
        pass


def decode_stream(
    command: tuple[str, ...],
    block_bytes: int,
    *,
    unit_bytes: int | None = None,
    deadline_seconds: float = DECODE_TIMEOUT_SECONDS,
) -> Generator[bytes, None, None]:
    """Run a decode under one wall deadline and yield its output in exact blocks.

    CRITICAL: the deadline starts before the child does and spans startup, every read and teardown.
    It cannot be expressed as a timeout on `process.wait`, and that is the defect this shape exists
    to prevent: reading stdout inline put `wait(timeout=...)` *after* a blocking `stream.read`, so
    a decoder that stalled with stdout open blocked the read indefinitely, the wait was never
    reached, and the `finally` could not run either -- an unbounded hang inside a tool whose whole
    contract is bounded observation. The read therefore happens on a separate daemon thread while
    this generator waits on a queue with the remaining time, so the deadline stays reachable no
    matter what the child does. A queue depth of one preserves the buffer bound: one video frame,
    or one second of PCM, is in flight at a time and none is retained -- counting the one the
    producer still holds, that is two, which is the real ceiling.

    CRITICAL, second half: teardown is *coordinated*, not hoped for. A consumer that stops early
    leaves the reader blocked in `put`, and neither killing the child nor closing its stdout can
    release a thread that is not blocked in `read`. Draining the queue once releases it once and it
    blocks again on the next block. So `stop` is set first, the queue is drained while the reader is
    joined with a bounded grace, and only then is the call finished. Removing the stop flag, the
    join, or the drain-while-joining loop leaks one daemon thread and its buffer per aborted
    artifact -- invisible in a single run and unbounded across a full qualification.
    """

    deadline = time.monotonic() + deadline_seconds
    process = subprocess.Popen(  # noqa: S603 -- fixed argv, digest-verified binary, no shell
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    sink: queue.Queue[Any] = queue.Queue(maxsize=1)
    stop = threading.Event()
    reader: threading.Thread | None = None
    try:
        if process.stdout is None:  # pragma: no cover -- Popen with PIPE always sets it
            raise QualificationError("the decoder produced no output stream")
        reader = threading.Thread(
            target=_pump_blocks,
            args=(process.stdout, block_bytes, unit_bytes, sink, stop),
            daemon=True,
        )
        reader.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationError("the decoder exceeded its per-artifact deadline")
            try:
                item = sink.get(timeout=remaining)
            except queue.Empty:
                raise QualificationError("the decoder exceeded its per-artifact deadline") from None
            if item is _DONE:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualificationError("the decoder exceeded its per-artifact deadline")
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise QualificationError("the decoder exceeded its per-artifact deadline") from None
        if status != 0:
            raise QualificationError("the decoder exited with a failure status")
    finally:
        # Order matters. The stop flag ends the producer's bounded `put` retry; killing the child
        # and closing the pipe end a blocking `read`; draining makes room for an item already in
        # flight. Only the join proves the thread is gone, and only draining *while* joining lets a
        # producer holding one more block finish handing it over and exit.
        stop.set()
        _terminate(process)
        if process.stdout is not None:
            process.stdout.close()
        grace = time.monotonic() + _TEARDOWN_GRACE_SECONDS
        while reader is not None and reader.is_alive() and time.monotonic() < grace:
            try:
                sink.get_nowait()
            except queue.Empty:
                reader.join(timeout=_TEARDOWN_POLL_SECONDS)
        while True:
            try:
                sink.get_nowait()
            except queue.Empty:
                break


#: The identity row reads as one of exactly two nominal colours (`FRAME_ID_ONE_RGB`/
#: `FRAME_ID_ZERO_RGB`, white/black); the same luma split `read_frame_id_tile` uses.
_FRAME_ID_LUMA_THRESHOLD = 128


def _read_identity(frame: bytes, width: int, height: int, read: IdentityRead) -> int | None:
    """The source frame index burned into this decoded frame, read at the prescribed cells.

    GUARD: the cells are prescribed by the composition's declared placement
    (`semantic_conformance_expect.identity_reads`), exactly as `patch_points` are, and are never
    located by searching the frame for the row's own colours. A colour search relocated the row
    onto whatever else in the frame matched -- a second clip sharing the primary's asset pulled
    the fit toward its own corner and read source frame 0 where the picture showed 16 -- and it
    failed outright wherever a blend or an effect moved the corner colours it searched for, which
    reported a correct picture as having no identity at all. Reading a prescribed pixel has no
    stake in where the layer is: a misplaced layer reads as garbage here and as a wrong rectangle
    in `geometry`, which is the right pair of answers. `None` only when a cell falls outside this
    decoded frame, which is a fact about the artifact's actual size.
    """

    value = 0
    for index, (x, y) in enumerate(read.cells):
        if not (0 <= x < width and 0 <= y < height):
            return None
        offset = (y * width + x) * RGB24_BYTES_PER_PIXEL
        red, green, blue = frame[offset], frame[offset + 1], frame[offset + 2]
        if (red + green + blue) // 3 >= _FRAME_ID_LUMA_THRESHOLD:
            value |= 1 << (FRAME_ID_BITS - 1 - index)
    return value


def _largest_component(points: Sequence[tuple[int, int]], *, stride: int) -> list[tuple[int, int]]:
    """The largest spatially-coherent group of same-colour matches, discarding the rest.

    GUARD: a genuine patch is a single dense block of matches; an unrelated clip that happens to
    share a colour (a different layer's own patch after its own blend/effect, a smaller one because
    it is scaled down, or a handful of stray pixels elsewhere) forms its own, separate, smaller
    group. Taking the bounding box of every match regardless of where it sits silently stretches
    the reported rectangle toward whatever else in the frame happened to match. Picking the single
    largest connected group instead is not a colour-based search for a placement (which the corpus
    forbids): it is a rejection rule against outliers, using only the matches' own spatial
    coherence, never a declared or expected position.
    """

    if not points:
        return []
    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x, y in points:
        grid.setdefault((x // stride, y // stride), []).append((x, y))
    visited: set[tuple[int, int]] = set()
    best: list[tuple[int, int]] = []
    for cell in grid:
        if cell in visited:
            continue
        stack = [cell]
        visited.add(cell)
        component: list[tuple[int, int]] = []
        while stack:
            current = stack.pop()
            component.extend(grid[current])
            cx, cy = current
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    neighbor = (cx + dx, cy + dy)
                    if neighbor in grid and neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)
        if len(component) > len(best):
            best = component
    return best


# ---------------------------------------------------------------------------------------------
# Geometry: where a layer, or one of its fiducials, actually landed -- a blind search, on
# purpose. `patches`/`color_patches` sample a *prescribed* point because a colour check must not
# be able to relocate itself onto whatever is actually there; geometry is the opposite question
# -- where a layer actually is -- so a blind search here is correct, not circular: a mispositioned
# layer is found wherever it actually landed and reported as such, which is what lets the join
# catch a real placement bug instead of confirming the placement it should be testing.
# ---------------------------------------------------------------------------------------------

#: The colours this corpus's synthetic media declare, plus the two the identity row uses. A
#: decoded pixel is assigned to whichever of these it is nearest, which is what lets a fiducial's
#: edge be found where the pixel stops being more like the patch than like anything else.
_MEDIA_PALETTE: tuple[tuple[int, int, int], ...] = tuple(
    dict.fromkeys(
        [
            *(profile.field for profile in SOURCE_PROFILES),
            *(patch.rgb() for profile in SOURCE_PROFILES for patch in profile.patches),
            FRAME_ID_ONE_RGB,
            FRAME_ID_ZERO_RGB,
        ]
    )
)
#: A layer counts as present in a frame when at least this many pixels classify to its own field
#: or one of its patches; fewer is stray encoder noise, not a layer.
_GEOMETRY_MIN_PRESENT_PIXELS = 6


def _nearest_palette(red: int, green: int, blue: int) -> int:
    best, best_distance = 0, None
    for index, (pr, pg, pb) in enumerate(_MEDIA_PALETTE):
        distance = (red - pr) ** 2 + (green - pg) ** 2 + (blue - pb) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = index, distance
    return best


def _classify_frame(
    frame: bytes, width: int, height: int
) -> tuple[list[int], tuple[int, int, int, int] | None]:
    """Every pixel's nearest palette colour (or -1 below the geometry luma threshold), plus the
    luma-threshold bounding box of the whole frame."""

    labels = [-1] * (width * height)
    min_x = min_y = None
    max_x = max_y = -1
    cache: dict[tuple[int, int, int], int] = {}
    for y in range(height):
        row_offset = y * width * RGB24_BYTES_PER_PIXEL
        for x in range(width):
            offset = row_offset + x * RGB24_BYTES_PER_PIXEL
            red, green, blue = frame[offset], frame[offset + 1], frame[offset + 2]
            if (red + green + blue) // 3 < GEOMETRY_LUMA_THRESHOLD:
                continue
            key = (red, green, blue)
            label = cache.get(key)
            if label is None:
                label = cache[key] = _nearest_palette(red, green, blue)
            labels[y * width + x] = label
            if min_x is None or x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if min_y is None or y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
    if min_x is None or min_y is None:
        return labels, None
    return labels, (min_x, min_y, max_x + 1, max_y + 1)


def _bbox(points: Sequence[tuple[int, int]]) -> tuple[int, int, int, int]:
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points) + 1,
        max(y for _, y in points) + 1,
    )


def _geometry_for_frame(
    frame: bytes, width: int, height: int, candidates: Sequence[tuple[str, SourceProfile]]
) -> list[dict[str, Any]]:
    """The layer rectangle and every patch fiducial actually found, for each candidate layer.

    `candidates` names which clip/profile pairs to look for -- literal composition data (a clip
    exists and which synthetic asset it uses), never a predicted rectangle.

    GUARD -- the whole-rectangle test is two steps, not one, and the order matters. Colour first,
    to confirm *identity*: a genuine cluster of pixels nearest this candidate's own declared field
    or patch colours proves this candidate's own material is actually visible in this frame, at
    all -- never a placement guess. Luma second, to measure *extent*: once identity is confirmed,
    the reported rectangle is the pure luma-threshold bounding box (no colour restriction), because
    that boundary survives a colour-adjust effect on the candidate's own pixels that the declared
    colour targets would not, and is what the corpus asked this stage to use for the rectangle.

    GUARD -- a fiducial is the largest connected group of pixels whose *nearest* palette colour is
    the patch's, not the pixels within a fixed distance of it. A scaled or rotated patch has a
    resampled edge a pixel or two wide whose colours sit between the patch and the field, and a
    fixed tolerance excluded that edge on both sides, so every such fiducial came back two to
    three pixels short of the rectangle the oracle stated, for a picture that was right. Nearest-
    colour assignment puts the boundary at the crossing, which is where the edge is. Never
    reintroduce a tolerance here, and never widen the frozen 2 px geometry bound instead.
    """

    labels, whole = _classify_frame(frame, width, height)
    landmarks: list[dict[str, Any]] = []
    for label, profile in candidates:
        own = {_MEDIA_PALETTE.index(profile.field)}
        own.update(_MEDIA_PALETTE.index(patch.rgb()) for patch in profile.patches)
        present = sum(1 for item in labels if item in own)
        if whole is not None and present >= _GEOMETRY_MIN_PRESENT_PIXELS:
            landmarks.append(
                {
                    "label": label,
                    "left": whole[0],
                    "top": whole[1],
                    "right": whole[2],
                    "bottom": whole[3],
                }
            )
        for patch in profile.patches:
            wanted = _MEDIA_PALETTE.index(patch.rgb())
            points = [
                (index % width, index // width)
                for index, item in enumerate(labels)
                if item == wanted
            ]
            component = _largest_component(points, stride=1)
            if len(component) < _GEOMETRY_MIN_PRESENT_PIXELS:
                continue
            fiducial = _bbox(component)
            landmarks.append(
                {
                    "label": f"{label}.{patch.label}",
                    "left": fiducial[0],
                    "top": fiducial[1],
                    "right": fiducial[2],
                    "bottom": fiducial[3],
                }
            )
    return landmarks


# ---------------------------------------------------------------------------------------------
# Alphas: the cross-dissolve ramp measured against its own two ends. A ramp's very first included
# frame and its steady-state frame are, by the declared alpha-progression policy, the ramp's own
# 0% and 100% (or 0% and the clip's own steady opacity) references -- both are pixels this exact
# decode already carries, so the ratio at any frame between them is measured, never predicted.
# Nothing here is derived for a clip with no active transition: a flat, unchanging opacity offers
# no second reference point inside one render to solve for, and reporting one anyway would mean
# either reading the declared basis-point value back (which is exactly the self-confirmation this
# stage exists to refuse) or guessing.
# ---------------------------------------------------------------------------------------------

#: The least summed squared travel, over the three channels, between a ramp's two ends for the
#: ratio to be reported at all: below this the pixel barely moved and any ratio is noise.
_ALPHA_MIN_TRAVEL_SQUARED = 16


def _alphas_for_clip(
    label: str,
    canvas_point: tuple[int, int],
    frame_samples: Mapping[int, bytes],
    width: int,
    height: int,
    base_frame: int,
    steady_frame: int,
    opacity_bp: int,
    sample_frames: Sequence[int],
) -> list[dict[str, Any]]:
    """Alpha at every named ramp frame, from pixels this decode already has at the ramp's two ends.

    `base` is what the ramp's first included frame actually measured (alpha is exactly 0 there by
    the declared progression, so the pixel *is* the un-mixed background); `mixed` is what the frame
    one past the ramp measured (the clip's true steady pixel, at whatever opacity it actually
    carries). Both are real samples of this decode, not values this function assumes.

    GUARD: the ratio `(observed - base) / (mixed - base)` is the ramp's own *progress* (0 at
    `base_frame`, 1 at `steady_frame`), not an absolute alpha -- the clip's declared opacity cancels
    out of that ratio algebraically no matter what it is, because `mixed` already carries whatever
    that opacity actually did to the pixel. Recovering the value the contract states means scaling
    the measured ratio by `opacity_bp` (a literal input field, read back the same way `blend` or
    `start_frame` already are, never the oracle's derived alpha). Do not drop this factor and
    report the bare ratio again: that regression previously shipped ramp-fraction values (e.g.
    1000 at the steady frame for every clip) where the contract states the ramp scaled by the
    clip's own opacity (e.g. 850 for an 8500bp clip) -- a real, reported defect, not a style choice.

    GUARD: the progress is one least-squares ratio over the three channels together -- the
    projection of the observed travel onto the full travel -- not a mean of three per-channel
    ratios. A per-channel mean weights a channel that moved by two code values the same as one
    that moved by a hundred, so the encoder's noise on the weak channel set the answer; measured
    at the overlay's field that reported 374 where the ramp stood at 425, a real, reported defect.
    The sample point itself is prescribed by the composition
    (`semantic_conformance_expect.alpha_sample_point`), chosen where the declared ramp moves the
    pixel most; this function never picks its own.
    """

    results: list[dict[str, Any]] = []
    if base_frame not in frame_samples or steady_frame not in frame_samples:
        return results
    base = patch_mean(
        frame_samples[base_frame],
        width,
        height,
        label=f"{label}.base",
        center_x=canvas_point[0],
        center_y=canvas_point[1],
        size_px=5,
    )
    mixed = patch_mean(
        frame_samples[steady_frame],
        width,
        height,
        label=f"{label}.mixed",
        center_x=canvas_point[0],
        center_y=canvas_point[1],
        size_px=5,
    )
    base_rgb = (base.red, base.green, base.blue)
    mixed_rgb = (mixed.red, mixed.green, mixed.blue)
    travel = [mixed_c - base_c for mixed_c, base_c in zip(mixed_rgb, base_rgb, strict=True)]
    travel_squared = sum(component * component for component in travel)
    if travel_squared < _ALPHA_MIN_TRAVEL_SQUARED:
        return results
    for frame_index in sample_frames:
        if frame_index not in frame_samples:
            continue
        if frame_index == base_frame:
            # Tautological (the pixel *is* base by definition) but still a real decode sample and
            # a real frame the contract names -- reporting 0 here closes that row rather than
            # leaving it absent, without pretending the ratio below was computed for it.
            results.append({"label": label, "output_frame": frame_index, "alpha_milli": 0})
            continue
        sample = patch_mean(
            frame_samples[frame_index],
            width,
            height,
            label=label,
            center_x=canvas_point[0],
            center_y=canvas_point[1],
            size_px=5,
        )
        observed_rgb = (sample.red, sample.green, sample.blue)
        progress = (
            sum(
                (observed_c - base_c) * travel_c
                for observed_c, base_c, travel_c in zip(observed_rgb, base_rgb, travel, strict=True)
            )
            / travel_squared
        )
        alpha_milli = round(progress * opacity_bp / 10)
        results.append({"label": label, "output_frame": frame_index, "alpha_milli": alpha_milli})
    return results


# ---------------------------------------------------------------------------------------------
# Audio: a blind onset scan over the whole decoded signal, labelled afterwards against the
# declared owner windows -- never a scan bounded to where an owner window says a burst should be,
# which would make a missing or drifted burst invisible instead of a reportable mismatch.
# ---------------------------------------------------------------------------------------------

#: A burst is full scale; anything crossing half scale is unambiguously a burst rather than
#: encoder ringing on an otherwise silent AAC frame.
_ONSET_THRESHOLD_RATIO = 0.5
#: Longer than one burst (256 samples) so a single burst's own decay tail is never counted twice.
_ONSET_REFRACTORY_SAMPLES = 512


def _scan_onsets(samples: bytes, *, threshold_ratio: float, refractory_samples: int) -> list[int]:
    """Every place the decoded signal crosses the threshold, with no assumption about how many.

    This is deliberately not `semantic_conformance_extract.detect_onsets`: that function raises
    unless the caller already knows the exact count to expect, which is exactly the assumption a
    blind scan must not make -- an extra or a missing burst has to come back as a different
    *count*, not as an exception this script would have to guess a label for.
    """

    threshold = int(threshold_ratio * PCM16_FULL_SCALE)
    total = len(samples) // PCM16_BYTES_PER_SAMPLE
    found: list[int] = []
    index = 0
    while index < total:
        offset = index * PCM16_BYTES_PER_SAMPLE
        value = int.from_bytes(samples[offset : offset + 2], "little", signed=True)
        if abs(value) >= threshold:
            found.append(index)
            index += refractory_samples + 1
            continue
        index += 1
    return found


def _label_onsets(
    positions: Sequence[int], owner_windows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Attach an `owner<N>_burst_<M>` label to each independently-detected onset.

    The *existence* and *position* of every entry came from `_scan_onsets` alone. `owner_windows`
    (each window's output-sample span and its shift from source to output) is literal composition
    scheduling data, supplied by the caller from the wire -- the same kind of fact
    `patch_sample_points` supplies for a colour sample -- used only to name which declared window
    and which declared burst a real, already-found onset falls inside. A position that falls in no
    window, or matches no declared burst start within a burst's own width, is labelled `unmatched`
    rather than forced onto the nearest one: a real extra or drifted burst must stay visible as a
    disagreement, not be laundered into matching whatever label was closest.
    """

    labelled: list[dict[str, Any]] = []
    for position in positions:
        label = "unmatched"
        for owner_index, window in enumerate(owner_windows):
            if not (window["start_sample"] <= position < window["end_sample"]):
                continue
            for burst_index, burst_start in enumerate(window["burst_starts"]):
                placed = burst_start + window["shift_samples"]
                if abs(position - placed) < window["burst_samples"]:
                    label = f"owner{owner_index}_burst_{burst_index}"
                    break
            break
        labelled.append({"label": label, "sample_index": position})
    return labelled


def _measure_silences(
    samples: bytes,
    positions: Sequence[int],
    last_sample: int,
    *,
    burst_samples: int,
    boundary_exclusion: int,
    first_frame_samples: int = 0,
    owner_windows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The measured peak in every interior gap between consecutive detected onsets.

    `abs_peak` is a real measurement of the decoded interior, at least `boundary_exclusion`
    samples clear of the burst on either side -- never the zero an expectation states for what
    *should* be there. A hard-coded zero here would make this whole check vacuous.

    GUARD: a burst detected inside its *source's* first codec frame (`first_frame_samples`, the
    declared codec's frame length, counted from the owner window's `shift_samples` -- where the
    source's sample 0 lands on the output) begins its gap at the end of that frame, not at its
    own end -- the same boundary `semantic_conformance_expect.derive_silences` declares, for the
    same reason: the source encoder had no run-up to switch windows on, the source itself rings
    past the burst, and the re-encode carries the tail one frame further wherever the clip places
    it (B-45 at output 0, B-61 at output 24000 for a second owner). Measure from a different
    boundary than the expectation declares and the row fails or passes on the codec, not on the
    product. Without owner windows the origin is the stream's start, which is the first owner's.
    """

    if not positions:
        return []
    ordered = sorted(positions)

    def origin(position: int) -> int:
        for window in owner_windows:
            if int(window["start_sample"]) <= position < int(window["end_sample"]):
                return int(window["shift_samples"])
        return 0

    boundaries = [
        max(position + burst_samples, origin(position) + first_frame_samples)
        if position - origin(position) < first_frame_samples
        else position + burst_samples
        for position in ordered
    ]
    ends = [*ordered[1:], last_sample]
    silences: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(boundaries, ends, strict=True)):
        interior_start = start + boundary_exclusion
        interior_end = end - boundary_exclusion
        if interior_end <= interior_start:
            continue
        silences.append(
            {
                "label": f"gap_{index}",
                "start_sample": start,
                "end_sample": end,
                "abs_peak": pcm16_peak(samples, interior_start, interior_end),
            }
        )
    return silences


def _audio_levels(samples: bytes, windows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The decoded signal's level over each prescribed window, against the declared tone.

    The windows are where the composition says to look (`audio_level_windows`); the level is the
    RMS this decode carries there, stated in millionths of the tone source's own RMS
    (`AUDIO_TONE_PEAK` over the square root of two). A window the decoded signal does not wholly
    contain is absent, never measured over the samples that happen to be there.
    """

    reference = AUDIO_TONE_PEAK / math.sqrt(2)
    levels: list[dict[str, Any]] = []
    for window in windows:
        start = int(window["start_sample"])
        count = int(window["sample_count"])
        try:
            rms = pcm16_rms(samples, start, count)
        except SemanticConformanceError:
            continue
        levels.append(
            {
                "label": str(window["label"]),
                "start_sample": start,
                "sample_count": count,
                "level_ppm": round(rms / reference * 1_000_000),
            }
        )
    return levels


# ---------------------------------------------------------------------------------------------
# Text: the declared content/font/weight/style/align are read back from the wire the way a case
# id is -- they are input specification, not a rendering outcome, and nothing short of OCR could
# verify glyph content independently within this stage's scope. What *is* independently measured
# is whether ink actually landed, and where: a blind search for the declared fill colour, because
# per-line bounds need real glyph metrics no analytic formula in this corpus has.
# ---------------------------------------------------------------------------------------------

#: Antialiased glyph edges blend toward the background, so a search for the declared fill colour
#: needs more headroom than a flat patch does.
_TEXT_FILL_TOLERANCE = 40


def _ink_bbox(
    frame: bytes, width: int, height: int, target: tuple[int, int, int], *, tolerance: int
) -> tuple[int, int, int, int] | None:
    """The bounding box of every pixel within `tolerance` of the declared fill colour, or `None`
    when the frame carries no such pixel -- a title that painted nothing is a real fact."""

    min_x = min_y = None
    max_x = max_y = -1
    for y in range(height):
        row_offset = y * width * RGB24_BYTES_PER_PIXEL
        for x in range(width):
            offset = row_offset + x * RGB24_BYTES_PER_PIXEL
            red, green, blue = frame[offset], frame[offset + 1], frame[offset + 2]
            if (
                abs(red - target[0]) <= tolerance
                and abs(green - target[1]) <= tolerance
                and abs(blue - target[2]) <= tolerance
            ):
                if min_x is None or x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if min_y is None or y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
    if min_x is None or min_y is None:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def _text_observation(
    text_target: Mapping[str, Any], frame_samples: Mapping[int, bytes], width: int, height: int
) -> dict[str, Any]:
    content = str(text_target.get("content", ""))
    line_count = content.count("\n") + 1
    fill = text_target.get("fill_rgba")
    sample_frames = text_target.get("sample_frames", ())
    ink_frame = next(
        (frame_samples[index] for index in sample_frames if index in frame_samples), None
    )
    line_bounds: list[dict[str, Any]] = []
    visible_glyph_ratio_milli = 0
    if ink_frame is not None and isinstance(fill, Sequence) and len(fill) >= 3:
        target = (int(fill[0]), int(fill[1]), int(fill[2]))
        whole = _ink_bbox(ink_frame, width, height, target, tolerance=_TEXT_FILL_TOLERANCE)
        if whole is not None:
            whole_left, whole_top, whole_right, whole_bottom = whole
            matched = 0
            area = max(1, (whole_right - whole_left) * (whole_bottom - whole_top))
            for y in range(whole_top, whole_bottom):
                row_offset = y * width * RGB24_BYTES_PER_PIXEL
                for x in range(whole_left, whole_right):
                    offset = row_offset + x * RGB24_BYTES_PER_PIXEL
                    red, green, blue = (
                        ink_frame[offset],
                        ink_frame[offset + 1],
                        ink_frame[offset + 2],
                    )
                    if (
                        abs(red - target[0]) <= _TEXT_FILL_TOLERANCE
                        and abs(green - target[1]) <= _TEXT_FILL_TOLERANCE
                        and abs(blue - target[2]) <= _TEXT_FILL_TOLERANCE
                    ):
                        matched += 1
            visible_glyph_ratio_milli = round(1000 * matched / area)
            # A measured approximation, not real per-glyph metrics: the whole discovered ink
            # region is split into `line_count` equal horizontal bands and each is re-searched for
            # its own tighter ink extent. A band with no ink of its own is omitted rather than
            # reported as the whole region's box, which would silently overstate the layout.
            band_height = max(1, (whole_bottom - whole_top) // max(1, line_count))
            for line_index in range(line_count):
                band_top = whole_top + line_index * band_height
                band_bottom = (
                    whole_bottom if line_index == line_count - 1 else band_top + band_height
                )
                if band_bottom <= band_top:
                    continue
                # Re-scan restricted to the band's rows only.
                band_left = band_top_found = None
                band_right = band_bottom_found = -1
                for y in range(band_top, band_bottom):
                    row_offset = y * width * RGB24_BYTES_PER_PIXEL
                    for x in range(width):
                        offset = row_offset + x * RGB24_BYTES_PER_PIXEL
                        red, green, blue = (
                            ink_frame[offset],
                            ink_frame[offset + 1],
                            ink_frame[offset + 2],
                        )
                        if (
                            abs(red - target[0]) <= _TEXT_FILL_TOLERANCE
                            and abs(green - target[1]) <= _TEXT_FILL_TOLERANCE
                            and abs(blue - target[2]) <= _TEXT_FILL_TOLERANCE
                        ):
                            if band_left is None or x < band_left:
                                band_left = x
                            if x > band_right:
                                band_right = x
                            if band_top_found is None or y < band_top_found:
                                band_top_found = y
                            if y > band_bottom_found:
                                band_bottom_found = y
                if band_left is not None and band_top_found is not None:
                    line_bounds.append(
                        {
                            "label": f"line_{line_index}",
                            "left": band_left,
                            "top": band_top_found,
                            "right": band_right + 1,
                            "bottom": band_bottom_found + 1,
                        }
                    )
    return {
        "content": content,
        "font_identity": str(text_target.get("font_asset_id", "")),
        "line_count": line_count,
        "weight": int(text_target.get("weight", 0)),
        "style": str(text_target.get("style", "")),
        "align": str(text_target.get("align", "")),
        "line_bounds": line_bounds,
        "visible_glyph_ratio_milli": visible_glyph_ratio_milli,
    }


def observe_artifact(
    ffmpeg: Path,
    ffprobe: Path,
    artifact: Path,
    *,
    deadline_seconds: float = DECODE_TIMEOUT_SECONDS,
    identity_reads: Sequence[IdentityRead] = (),
    patch_points: Sequence[SamplePoint] = (),
    geometry_targets: Sequence[GeometryTarget] = (),
    alpha_targets: Sequence[Mapping[str, Any]] = (),
    text_target: Mapping[str, Any] | None = None,
    audio_owner_windows: Sequence[Mapping[str, Any]] = (),
    audio_level_windows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Observe the artifact-level facts, decoding rather than trusting the container's hints.

    One deadline is shared across the two probes and the two decodes, so the per-artifact bound is
    a bound on observing the artifact rather than on each invocation separately.

    Every "where to look" argument is a prescription derived from the declared composition by
    `semantic_conformance_expect` and handed in by the caller -- the identity cells per output
    frame, one canvas point per colour label at the frame it belongs to, which layers to search
    for and at which frame, where a ramp is measured. This function still only ever reports the
    value it actually decodes there: it never predicts, and never reads, what that value was
    supposed to be. Sampling a prescribed point is deliberately not the same operation as the
    geometry search below -- searching for a patch by its own colour would make a *mispositioned*
    layer look correctly placed as long as its colour survived, which is exactly what a `geometry`
    or `patches` landmark exists to catch.
    """

    deadline = time.monotonic() + deadline_seconds

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise QualificationError("observing the artifact exceeded its deadline")
        return left

    probe = run_probe(probe_command(ffprobe, artifact), deadline_seconds=remaining())
    metadata = parse_output_metadata(probe)
    timing = parse_frame_timing(
        run_probe(timing_probe_command(ffprobe, artifact), deadline_seconds=remaining())
    )
    frame_bytes = video_frame_bytes(metadata.width, metadata.height)
    frames = 0
    source_mapping: list[dict[str, Any]] = []
    reads_by_frame = {read.output_frame: read for read in identity_reads}
    #: Every point is sampled at the frame its own label names -- a ramp's `@frame` points at the
    #: ramp's midpoint, the quiet frame's points there -- never all at the first point's frame.
    #: Sampling them all at one frame was a real, reported defect: every `@14` point was read at
    #: frame 18 and compared against the colour stated for frame 14.
    points_by_frame: dict[int, list[SamplePoint]] = {}
    for point in patch_points:
        points_by_frame.setdefault(point.output_frame, []).append(point)
    patches: list[dict[str, Any]] = []
    geometry: list[dict[str, Any]] = []
    #: Each layer is searched for at the frame the composition names for it (the frame with the
    #: fewest other clips active), not at a fixed frame 0 -- see `geometry_sample_frame`.
    candidates_by_frame: dict[int, list[tuple[str, SourceProfile]]] = {}
    for geometry_target in geometry_targets:
        profile = profile_for(geometry_target.asset_id)
        if profile is None:
            continue
        candidates_by_frame.setdefault(geometry_target.sample_frame, []).append(
            (geometry_target.clip_id, profile)
        )
    #: Every decoded frame an alpha ramp actually needs, gathered as the decode passes it rather
    #: than requiring a second decode -- a handful of raw rgb24 frames for a corpus capped at 384
    #: frames is not the "whole decoded video" the module docstring's bound is about.
    alpha_wanted_frames = {
        frame_index for target in alpha_targets for frame_index in target.get("sample_frames", ())
    }
    alpha_frame_samples: dict[int, bytes] = {}
    text_frame_samples: dict[int, bytes] = {}
    text_wanted_frames = set(text_target.get("sample_frames", ())) if text_target else set()
    for block in decode_stream(
        decode_video_command(ffmpeg, artifact), frame_bytes, deadline_seconds=remaining()
    ):
        # Counted, not retained: one frame is in hand at a time, examined and then discarded --
        # except the handful of frames named above, which are copied out because a ramp or a
        # text span needs more than one frame's pixels to say anything.
        read = reads_by_frame.get(frames)
        if read is not None:
            source_frame = _read_identity(block, metadata.width, metadata.height, read)
            if source_frame is not None:
                source_mapping.append(
                    {
                        "output_frame": frames,
                        "source_frame": source_frame,
                        # The asset's own declared table, applied to the index this decode read.
                        # `None` when the index read is one the table does not name, which the
                        # join reports as the index mismatch it is rather than as a time.
                        "source_pts": read.pts_by_frame.get(source_frame),
                        "source_time_base_num": read.time_base_num,
                        "source_time_base_den": read.time_base_den,
                    }
                )
        for point in points_by_frame.get(frames, ()):
            try:
                sample = patch_mean(
                    block,
                    metadata.width,
                    metadata.height,
                    label=point.label,
                    center_x=point.canvas_x,
                    center_y=point.canvas_y,
                    size_px=5,
                )
            except SemanticConformanceError:
                # A declared point that does not admit a 5x5 interior sample on *this* decoded
                # artifact (too close to the actual frame edge) is a fact about the artifact, not
                # a crash: the point is simply absent from what follows.
                continue
            patches.append(
                {
                    "label": sample.label,
                    "size_px": sample.size_px,
                    "edge_distance_px": sample.edge_distance_px,
                    "red": sample.red,
                    "green": sample.green,
                    "blue": sample.blue,
                }
            )
        candidates = candidates_by_frame.get(frames)
        if candidates:
            geometry.extend(_geometry_for_frame(block, metadata.width, metadata.height, candidates))
        if frames in alpha_wanted_frames:
            alpha_frame_samples[frames] = block
        if frames in text_wanted_frames:
            text_frame_samples[frames] = block
        frames += 1
    grid = parse_frame_grid(probe, frames, timing)

    alphas: list[dict[str, Any]] = []
    for target in alpha_targets:
        sample_frames = target.get("sample_frames")
        canvas_point = target.get("canvas_point")
        label = target.get("label")
        base_frame = target.get("base_frame")
        steady_frame = target.get("steady_frame")
        opacity_bp = target.get("opacity_bp")
        if (
            not sample_frames
            or canvas_point is None
            or label is None
            or base_frame is None
            or steady_frame is None
            or opacity_bp is None
        ):
            continue
        alphas.extend(
            _alphas_for_clip(
                str(label),
                (int(canvas_point[0]), int(canvas_point[1])),
                alpha_frame_samples,
                metadata.width,
                metadata.height,
                int(base_frame),
                int(steady_frame),
                int(opacity_bp),
                [int(frame_index) for frame_index in sample_frames],
            )
        )

    text_observation: dict[str, Any] | None = None
    if text_target is not None:
        text_observation = _text_observation(
            text_target, text_frame_samples, metadata.width, metadata.height
        )

    audio_samples: int | None = None
    audio_onsets: list[dict[str, Any]] = []
    silences: list[dict[str, Any]] = []
    audio_levels: list[dict[str, Any]] = []
    if metadata.audio_stream_count:
        decoded = 0
        # CRITICAL: unlike the video loop above, the decoded PCM is concatenated rather than
        # counted-and-discarded. A blind onset scan and an interior silence measurement both need
        # to see the signal as one continuous timeline -- an onset a block boundary happens to
        # split, or a gap that straddles two one-second blocks, would be invisible to a per-block
        # scan. This corpus caps a case at a few seconds of mono 16-bit audio (low tens of KB), so
        # retaining it whole is not the unbounded "whole decoded video" this module's docstring
        # forbids; it is the only way to scan a signal rather than a sequence of disconnected
        # fragments of it.
        audio_buffer = bytearray()
        audio_blocks = decode_stream(
            decode_audio_command(ffmpeg, artifact),
            audio_block_bytes(),
            unit_bytes=PCM16_BYTES_PER_SAMPLE,
            deadline_seconds=remaining(),
        )
        for block in audio_blocks:
            decoded += len(block) // PCM16_BYTES_PER_SAMPLE
            audio_buffer.extend(block)
        audio_samples = decoded
        if audio_owner_windows:
            positions = _scan_onsets(
                bytes(audio_buffer),
                threshold_ratio=_ONSET_THRESHOLD_RATIO,
                refractory_samples=_ONSET_REFRACTORY_SAMPLES,
            )
            audio_onsets = _label_onsets(positions, audio_owner_windows)
            last_sample = max(
                (int(window["end_sample"]) for window in audio_owner_windows), default=audio_samples
            )
            silences = _measure_silences(
                bytes(audio_buffer),
                positions,
                last_sample,
                burst_samples=int(audio_owner_windows[0].get("burst_samples", 0)),
                boundary_exclusion=TOLERANCE_PROFILE.codec_boundary_exclusion_samples,
                first_frame_samples=int(audio_owner_windows[0].get("codec_frame_samples", 0)),
                owner_windows=audio_owner_windows,
            )
        if audio_level_windows:
            audio_levels = _audio_levels(bytes(audio_buffer), audio_level_windows)

    return {
        "artifact_observation": "h3.context.nle_semantic_final_observation.v1",
        "extractor_fingerprint": extractor_fingerprint(),
        "output_metadata": dict(metadata.as_pairs()),
        "frame_grid": {
            "width": grid.width,
            "height": grid.height,
            "frame_count": grid.frame_count,
            "frame_rate_num": grid.frame_rate_num,
            "frame_rate_den": grid.frame_rate_den,
            "duration_seconds": str(grid.duration()),
        },
        "frame_timing": dict(timing.as_pairs()),
        # CRITICAL: the summary pairs above are derived values. The raw table is emitted too,
        # because a consumer that rebuilds a sequence from a count and a uniform step rebuilds
        # a *clean* one -- strictly monotonic, no duplicates, no negatives -- and would launder
        # away the very anomalies F4 exists to catch. Anyone comparing timing must use this.
        "frame_timing_table": {
            "time_base_num": timing.time_base_num,
            "time_base_den": timing.time_base_den,
            "pts_ticks": list(timing.pts_ticks),
            "dts_ticks": list(timing.dts_ticks),
            "duration_ticks": list(timing.duration_ticks),
        },
        "audio_extent_samples": audio_samples,
        # CRITICAL: every index here is read out of this decode's own pixels at the cells the
        # declared placement prescribes -- never out of the composition, a render receipt or a
        # plan -- and the only declared thing attached to it is the asset's own landmark table,
        # which turns the index read into the source time that index carries. A frame whose cells
        # fall outside the decoded picture is absent from this list rather than guessed; an empty
        # list means no prescribed cell lay inside any decoded frame, which is itself a real,
        # reportable fact about the artifact rather than a failure of this function.
        "source_mapping": source_mapping,
        # Both keys carry the exact same measured samples, at the exact points and frames
        # `patch_sample_points` declared. Nothing in that function's output marks a point as
        # belonging to the opacity/blend family (`patches`) versus the colour-adjust family
        # (`color_patches`), so this reports the one measurement twice rather than guessing a
        # split the source data does not carry.
        "patches": patches,
        "color_patches": patches,
        # A blind luma bounding-box search per candidate layer and a nearest-colour search per
        # patch fiducial, each at the frame the composition names for that layer -- see
        # `_geometry_for_frame`'s own docstring for why a search is correct here and wrong for
        # `patches` above. Absent entries are layers this decode could not locate at all.
        "geometry": geometry,
        # Measured only where a real second reference exists inside this one decode: a clip's own
        # transition ramp, at the point the composition names as loudest for it. A flat,
        # non-transitioning clip's alpha is not stated here at all -- the caller derives ramp
        # frames from the clip's own transition fields; a flat clip supplies none, and one frame
        # cannot supply what a ramp needs.
        "alphas": alphas,
        "text": text_observation,
        # `_scan_onsets` finds every real threshold crossing first, with no assumption about how
        # many exist; `_label_onsets` then names each one against the declared owner windows, and
        # a position matching no declared window or burst is labelled `unmatched` rather than
        # forced onto the nearest label.
        "audio_onsets": audio_onsets,
        "silences": silences,
        # M25-77: the level of every prescribed window, read from this decode alone.
        "audio_levels": audio_levels,
    }


def build_report(
    outcomes: Sequence[CaseOutcome],
    *,
    candidate_tree: str,
    renderer_fingerprint: str,
    probe_fingerprint: str,
    extractor_fingerprint: str,
    browser_profile_fingerprint: str,
    chromium_version: str,
) -> dict[str, Any]:
    """Assemble the closed report. Statuses are counted, never averaged into a score."""

    corpus = build_corpus()
    required = len(corpus.cases)
    summary = summarize(outcomes, required_rows=required)
    # CRITICAL: admission is a join on identity, and it has to happen here rather than inside
    # `summarize`. Status totals cannot distinguish 306 executed rows from one row repeated 306
    # times, so a duplicating runner used to emit `conformant: true` having omitted 305 required
    # cases. Never make `conformant` a function of the totals alone again.
    admission = admit_report(corpus, [(item.case_id, item.status) for item in outcomes])
    rows = [outcome.as_wire() for outcome in outcomes]
    for row, outcome in zip(rows, outcomes, strict=True):
        encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CASE_PAYLOAD_BYTES:
            raise QualificationError(
                f"the derived payload for {outcome.case_id} exceeds its per-case budget"
            )
    report = {
        "schema": CONFORMANCE_REPORT_SCHEMA,
        "corpus_version": CORPUS_VERSION,
        "tolerance_version": TOLERANCE_VERSION,
        "tolerance": TOLERANCE_PROFILE.as_wire(),
        "pins": {
            "candidate_tree": candidate_tree,
            "renderer_fingerprint": renderer_fingerprint,
            "probe_fingerprint": probe_fingerprint,
            "extractor_fingerprint": extractor_fingerprint,
            "browser_profile_fingerprint": browser_profile_fingerprint,
            "chromium_version": chromium_version,
        },
        "required_rows": required,
        "executed_rows": summary.executed_rows,
        "counts_by_class": corpus.counts_by_class(),
        "totals": dict(summary.totals),
        "admission": admission.as_wire(),
        "conformant": summary.conformant() and admission.admitted(),
        "rows": rows,
    }
    encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise QualificationError("the aggregate report exceeds its byte budget")
    return report


def extractor_fingerprint() -> str:
    """Hash this script together with the pure extractor it drives.

    Both halves are pinned because either one changing changes what "independently extracted" meant
    for a given report.
    """

    reader = hashlib.sha256()
    for path in (
        Path(__file__),
        ROOT / "comfyui_h3_context" / "core" / "semantic_conformance_extract.py",
    ):
        reader.update(path.read_bytes())
    return f"sha256:{reader.hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, help="path to the pinned FFmpeg binary")
    parser.add_argument("--ffprobe", type=Path, help="path to the pinned ffprobe binary")
    parser.add_argument("--artifact", type=Path, help="the verified final artifact to observe")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print the resolved runtime pins and corpus totals without decoding",
    )
    args = parser.parse_args(argv)

    try:
        if args.describe:
            corpus = build_corpus()
            print(
                json.dumps(
                    {
                        "corpus_version": CORPUS_VERSION,
                        "tolerance_version": TOLERANCE_VERSION,
                        "counts_by_class": corpus.counts_by_class(),
                        "required_rows": len(corpus.cases),
                        "extractor_fingerprint": extractor_fingerprint(),
                        "renderer_fingerprint": f"sha256:{_expected_digest('renderer')}",
                        "probe_fingerprint": f"sha256:{_expected_digest('probe')}",
                    },
                    indent=2,
                )
            )
            return 0
        if args.artifact is None:
            parser.error("--artifact is required unless --describe is given")
        ffprobe = resolve_binary(args.ffprobe, "probe")
        ffmpeg = resolve_binary(args.ffmpeg, "renderer")
        observation = observe_artifact(ffmpeg, ffprobe, args.artifact)
        print(json.dumps(observation, indent=2))
        return 0
    except (QualificationError, SemanticConformanceError) as exc:
        # Blocked, not failed: the run produced no observation, and that is what gets reported.
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
