"""M25-20 corrective: the RENDER + INDEPENDENT EXTRACTION stage.

Turns every rendering row of the closed semantic conformance corpus into a real artifact through
the accepted render path, then observes that artifact with the independent extractor in
``scripts/nle_semantic_conformance.py``. This module never reads a render receipt, the render
planner, or the render graph to obtain an observed value -- ``observe_artifact`` decodes the file
bytes with the pinned decoder, exactly as it does for every other caller.

Scope: this script builds the composition, drives it through
``AuthoringOutputRegistry.create -> status -> open_download`` (the accepted M25-19 transport,
which itself runs ``prepare_bound_render_plan -> acquire_render_job_sources ->
prepare_render_assets -> run_prepared_render -> measure_render_output`` inside the product's
render service and serves only a completely verified body), and then observes the artifact
independently. It does not compare the observation against an expectation and does not judge
per-case correctness; that is a later stage's job. One JSON record is written per corpus row.

Runtime pins: the renderer and probe are supplied only through
``H3_CONTEXT_AUTHORIZED_FFMPEG_PATH`` and ``H3_CONTEXT_AUTHORIZED_FFPROBE_PATH`` (or the matching
``--ffmpeg``/``--ffprobe`` flags) and are digest-verified against the packaged renderer
qualification, the same mechanism the real-media test suite already uses. There is no PATH
discovery and no hardcoded tool path in this file.

Fixture identity: the accepted base composition reuses the exact clip/track/asset identifiers that
``comfyui_h3_context.core.semantic_conformance_cases`` hardcodes (``clip-main``, ``vid-primary``,
and so on), because ``apply_edits`` locates wire elements by those literal strings. The one
wrinkle is the font: the corpus's own base fixture
(``tests/fixtures/m25_10_composition_contract_v1.json``) uses a placeholder font asset id
(``font-main``) that is never a real packaged font. This script leaves that placeholder in its own
base wire untouched and instead relies on ``semantic_conformance_cases.normalize_base`` -- called
once per row, inside ``apply_edits`` -- to rename it to the real packaged font id before any row is
decoded, exactly the way ``scripts/nle_semantic_qualify.py`` does for the backend qualification
stage. See ``_build_case_snapshots`` below for where that call happens.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Final, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.adapters.authoring_fonts import (  # noqa: E402
    AuthoringFontError,
    PackagedFontManifest,
    load_packaged_font_manifest,
)
from comfyui_h3_context.adapters.authoring_image_source import ImageSourcePool  # noqa: E402
from comfyui_h3_context.adapters.authoring_native_renderer import (  # noqa: E402
    NativeAuthoringRenderer,
)
from comfyui_h3_context.adapters.authoring_output_service import (  # noqa: E402
    AuthoringOutputRegistry,
)
from comfyui_h3_context.adapters.authoring_render_executor import (  # noqa: E402
    RenderPreparationError,
)
from comfyui_h3_context.adapters.authoring_render_leases import (  # noqa: E402
    RenderSourceLeaseError,
)
from comfyui_h3_context.adapters.authoring_render_probe import (  # noqa: E402
    RenderProbeError,
)
from comfyui_h3_context.adapters.authoring_render_process import (  # noqa: E402
    PinnedRenderExecutable,
    RenderProcessError,
    pin_render_executable,
)
from comfyui_h3_context.adapters.authoring_render_service import (  # noqa: E402
    AuthoringRenderService,
    RenderServiceError,
)
from comfyui_h3_context.adapters.authoring_render_source import (  # noqa: E402
    AuthoringRenderSourceClaim,
    BoundAuthoringRenderPlan,
    PreparedAuthoringHistory,
    claim_render_source,
    prepare_bound_render_plan,
)
from comfyui_h3_context.adapters.authoring_render_store import (  # noqa: E402
    RenderOutputStore,
    RenderStoreError,
)
from comfyui_h3_context.adapters.authoring_source_binding import (  # noqa: E402
    AuthoringSourceBindingError,
    AuthoringSourceBindingReceipt,
    ComfyVideoInputTypeAuthority,
    PathBackedComfyVideoFromFileV1Factory,
    RuntimeComfySourceFactory,
)
from comfyui_h3_context.adapters.av_reconstruction_media import (  # noqa: E402
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_media_preview import (  # noqa: E402
    clear_authoring_media_preview_adapter,
    publish_authoring_media_preview_adapter,
)
from comfyui_h3_context.adapters.comfyui_authoring_workspace import (  # noqa: E402
    AuthoringDispatchResult,
    AuthoringWorkbenchError,
)
from comfyui_h3_context.core.authoring_output_protocol import (  # noqa: E402
    OUTPUT_CREATE_SCHEMA,
    AuthoringOutputCreate,
    OutputProtocolError,
)
from comfyui_h3_context.core.composition_contract import (  # noqa: E402
    OUTPUT_PROFILE_ID,
    PublicCompositionSnapshot,
    decode_public_snapshot,
)
from comfyui_h3_context.core.contracts import AssetRole, MediaKind  # noqa: E402
from comfyui_h3_context.core.errors import (  # noqa: E402
    ContractValidationError,
    ReferenceRegistryError,
)
from comfyui_h3_context.core.registry import (  # noqa: E402
    ReferenceAsset,
    ReferenceRegistry,
    build_reference_registry,
)
from comfyui_h3_context.core.render_planner import RenderPlannerError  # noqa: E402
from comfyui_h3_context.core.semantic_conformance import (  # noqa: E402
    ACCEPTED_ARTIFACT_RETRIEVAL,
    SemanticConformanceError,
)
from comfyui_h3_context.core.semantic_conformance_cases import (  # noqa: E402
    BASE_FIXTURE_NAMES,
    BASE_NAMES,
    CLIP_AUDIO_BASE,
    CORPUS_BASE,
    HIGH_RESOLUTION_BASE,
    IMAGE_ASSET,
    PRIMARY_ASSET,
    CaseRecipe,
    RecipeBook,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_drive import (  # noqa: E402
    DriveError,
    command_phases,
    run_setup,
)
from comfyui_h3_context.core.semantic_conformance_expect import (  # noqa: E402
    CODEC_FRAME_SAMPLES,
    ExpectationError,
    GeometryTarget,
    IdentityRead,
    SamplePoint,
    alpha_targets,
    audio_level_windows,
    geometry_targets,
    identity_reads,
    patch_sample_points,
    source_start_tick,
)
from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    AUDIO_BURST_SAMPLES,
    AUDIO_BURST_STARTS,
    AUDIO_SAMPLE_RATE,
    AUDIO_TONE_HZ,
    AUDIO_TONE_PEAK,
    FRAME_ID_ONE_RGB,
    FRAME_ID_ZERO_RGB,
    HIGH_RES_IMAGE_ASSET,
    HIGH_RES_OVERLAY_ASSET,
    HIGH_RES_PRIMARY_ASSET,
    OVERLAY_ASSET,
    SOURCE_TIME_BASE_DEN,
    TIMING_ASSET,
    TONE_ASSET,
    SourceProfile,
    frame_id_bits,
    imported_source,
    imported_source_landmarks_match,
    profile_for,
)
from scripts import nle_semantic_conformance as extractor  # noqa: E402
from scripts import nle_semantic_import_scenario as import_scenario  # noqa: E402

#: The output canvas itself is never overridden -- it is read from the fixture's own declared
#: ``output.width``/``output.height`` (320x180), one of the finalized plan's two default
#: synthetic-media canvases -- so every row renders against the exact composition it is a case
#: of, and an ``output.*`` dimension-admission row still exercises the dimension it names.
#:
#: The *source* media's own pixel geometry -- each profile's own `width`/`height`, which is
#: `SOURCE_WIDTH`/`SOURCE_HEIGHT` times its declared scale --
#: ``OVERLAY_ASSET`` and every patch/frame-identity/audio-burst constant come from
#: ``comfyui_h3_context.core.semantic_conformance_media`` -- imported above, never restated here
#: -- because the independent extractor and the report join read landmarks back out of this exact
#: same media against this exact same spec; a locally-chosen number here would silently stop
#: being the number either of them expects.
SOURCE_FPS = 24
#: The source time base (1/12288) and the tick arithmetic every declared landmark table rests on
#: live in ``semantic_conformance_media`` (``SOURCE_TIME_BASE_DEN``), imported above: the render
#: stage encodes at exactly that container track timescale so that the file's measured timing is
#: the base's declared timing rather than a restatement of it. Leave ``-enc_time_base:v`` at the
#: source's frame rate for a CFR encode: it is the encoder's internal frame-rate assumption, a
#: different knob from the container's track timescale, and setting it to 1/12288 for a CFR
#: source (confirmed empirically) silently turns a 24fps encode into a garbage 12288fps one --
#: re-check ``ffprobe -show_entries stream=r_frame_rate`` after touching either flag.
DECODE_TIMEOUT_SECONDS = 120

FIXTURES_DIR = ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURES_DIR / BASE_FIXTURE_NAMES[CORPUS_BASE]
#: Every video source a base builds, binds and claims, in the order that base declares them, and
#: the one image beside them.
#:
#: GUARD: one runtime per base, never one registry holding both. `MAX_REFERENCE_VIDEOS` is 3 and
#: the accepted base already uses all three, so a shared registry carrying the high-resolution
#: pair as well would be refused by the product -- correctly. The stage therefore builds a base's
#: sources, registry and claims together and tears them down before the next base, which is also
#: what keeps a row's claim bound to the picture its own composition names.
BASE_VIDEO_ASSETS: Final[Mapping[str, tuple[str, ...]]] = {
    CORPUS_BASE: (PRIMARY_ASSET, OVERLAY_ASSET, TIMING_ASSET),
    HIGH_RESOLUTION_BASE: (HIGH_RES_PRIMARY_ASSET, HIGH_RES_OVERLAY_ASSET),
    CLIP_AUDIO_BASE: (TONE_ASSET,),
}
BASE_IMAGE_ASSET: Final[Mapping[str, str]] = {
    CORPUS_BASE: IMAGE_ASSET,
    HIGH_RESOLUTION_BASE: HIGH_RES_IMAGE_ASSET,
    CLIP_AUDIO_BASE: IMAGE_ASSET,
}
REPORT_SCHEMA = "h3.context.nle_semantic_render_report_row.v1"


class RenderStageError(RuntimeError):
    """One bounded, code-only failure to render or observe a single row's phase."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# A phase failure is any typed, content-free exception this pipeline already raises for a refused
# or blocked condition. Catching this tuple (rather than bare ``Exception``) keeps a genuine
# programming defect in this script visible as a crash instead of a silently reported row.
_PHASE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RenderStageError,
    OutputProtocolError,
    RenderServiceError,
    AuthoringWorkbenchError,
    AuthoringSourceBindingError,
    RenderSourceLeaseError,
    RenderPreparationError,
    RenderProcessError,
    RenderProbeError,
    RenderStoreError,
    AuthoringFontError,
    ContractValidationError,
    ReferenceRegistryError,
    RenderPlannerError,
    SemanticConformanceError,
    extractor.QualificationError,
    AVMediaAdapterError,
    DriveError,
    ExpectationError,
)


def _phase_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "unclassified_error"


class Control:
    """The minimal ``ProcessControl`` shape the adapters require, one deadline per phase."""

    def __init__(self, timeout_seconds: float) -> None:
        self.deadline = time.monotonic() + timeout_seconds

    def is_cancelled(self) -> bool:
        return False


# ---------------------------------------------------------------------------------------------
# A fake, method-free ComfyUI VideoInput authority, so this script never imports comfy_api or
# starts a ComfyUI host. This is the same construction the real-media test suite uses.
# ---------------------------------------------------------------------------------------------

_VideoInput = type("VideoInput", (), {"__module__": "comfy_api.latest._input.video_types"})
_VideoFromFile = type(
    "VideoFromFile", (_VideoInput,), {"__module__": "comfy_api.latest._input_impl.video_types"}
)
_VideoFromComponents = type(
    "VideoFromComponents",
    (_VideoInput,),
    {"__module__": "comfy_api.latest._input_impl.video_types"},
)
_TYPE_AUTHORITY = ComfyVideoInputTypeAuthority(
    video_input=_VideoInput,
    video_from_file=_VideoFromFile,
    video_from_components=_VideoFromComponents,
)


def _file_video(name: str) -> object:
    video = _VideoFromFile()
    object.__setattr__(video, "_VideoFromFile__file", name)
    return video


# ---------------------------------------------------------------------------------------------
# Real synthetic source media, built once with the pinned renderer.
# ---------------------------------------------------------------------------------------------


def _paint_square(
    frame: bytearray,
    width: int,
    height: int,
    *,
    left: int,
    top: int,
    size: int,
    rgb: tuple[int, int, int],
) -> None:
    """Fill an axis-aligned square with one flat colour, clipping to the frame's own bounds."""

    red, green, blue = rgb
    for y in range(max(0, top), min(height, top + size)):
        row = y * width * 3
        for x in range(max(0, left), min(width, left + size)):
            offset = row + x * 3
            frame[offset] = red
            frame[offset + 1] = green
            frame[offset + 2] = blue


def _paint_frame(profile: SourceProfile, frame_index: int) -> bytearray:
    """Paint one source frame exactly as ``semantic_conformance_media`` specifies it.

    Order matters and matches the module docstring's layering: the flat field first, the identity
    row second (only when the profile carries one), then every patch -- the same order the module
    docstring gives, and the only order that leaves every mark actually visible in the frame this
    function returns.
    """

    # A profile states its own size, because a high-resolution source is a larger picture rather
    # than the same picture enlarged: reading the module constants here would paint a 128-square
    # of marks into a 640-square frame and leave the rest flat field.
    width, height = profile.width, profile.height
    frame = bytearray(profile.field * (width * height))
    if profile.carries_frame_id:
        origin_x, origin_y = profile.frame_id_origin_px
        pitch = profile.frame_id_cell_pitch_px
        mark = profile.frame_id_mark_px
        for index, bit in enumerate(frame_id_bits(frame_index)):
            rgb = FRAME_ID_ONE_RGB if bit else FRAME_ID_ZERO_RGB
            _paint_square(
                frame,
                width,
                height,
                left=origin_x + index * pitch,
                top=origin_y,
                size=mark,
                rgb=rgb,
            )
    for patch in profile.patches:
        _paint_square(
            frame,
            width,
            height,
            left=patch.left,
            top=patch.top,
            size=patch.size,
            rgb=patch.rgb(),
        )
    return frame


def _build_image_tensor(profile: SourceProfile) -> Any:
    """The image reference source's pixels, painted from its profile like any other source frame.

    ``profile.carries_frame_id`` is ``False`` for the image profile, so ``frame_index`` never
    matters here -- passed as 0 only because ``_paint_frame`` always takes one.
    """

    # CRITICAL: keep torch lazy. The import-integration fixture reuses the source-video
    # builder in this module and must not require the optional image-render dependency.
    import torch

    frame = _paint_frame(profile, 0)
    pixels = torch.frombuffer(frame, dtype=torch.uint8).reshape(1, profile.height, profile.width, 3)
    return pixels.to(dtype=torch.float32) / 255.0


def _build_audio_pcm(sample_count: int, kind: str = "bursts") -> bytes:
    """A source's embedded audio as mono PCM16, of the kind its profile declares.

    ``bursts``: silence, except ``AUDIO_BURST_SAMPLES`` of a full-scale tone at each declared burst
    start. A pure tone rather than a flat DC pulse: encoders attenuate a sustained DC level, and a
    tone inside the ordinary audible band survives lossy AAC encoding as a locatable, still-loud
    onset. Phase is cosine so the very first sample of a burst is already at full scale, keeping
    the measured onset at (or a handful of samples after) the declared start rather than a quarter
    cycle into it.

    ``tone``: ``AUDIO_TONE_HZ`` at ``AUDIO_TONE_PEAK`` from the first sample to the last -- one
    period repeated, so every window of whole periods carries exactly the declared level.
    """

    import math
    import struct

    if kind == "tone":
        period, remainder = divmod(AUDIO_SAMPLE_RATE, AUDIO_TONE_HZ)
        if remainder:
            raise RenderStageError("tone_period_not_whole")
        cycle = [
            round(AUDIO_TONE_PEAK * math.sin(2.0 * math.pi * index / period))
            for index in range(period)
        ]
        values = [cycle[index % period] for index in range(sample_count)]
        return struct.pack(f"<{sample_count}h", *values)
    if kind != "bursts":
        raise RenderStageError("audio_kind_undeclared")

    pcm = bytearray(sample_count * 2)
    tone_hz = 1_000.0
    for start in AUDIO_BURST_STARTS:
        if start >= sample_count:
            continue
        end = min(start + AUDIO_BURST_SAMPLES, sample_count)
        for offset in range(end - start):
            angle = 2.0 * math.pi * tone_hz * offset / AUDIO_SAMPLE_RATE
            value = max(-32_768, min(32_767, round(32_767 * math.cos(angle))))
            struct.pack_into("<h", pcm, (start + offset) * 2, value)
    return bytes(pcm)


def _vfr_setpts(profile: SourceProfile) -> str:
    """A `setpts` filter stamping frame N with the declared tick at which it begins.

    The declared timing is segments of a repeating cycle (`SourceProfile.timing_segments`), so
    the tick of frame N inside a segment beginning at frame S and tick T with cycle `c` is
    `T + floor((N - S) / len(c)) * sum(c) + partial((N - S) mod len(c))`, and the segments nest
    as `if(lt(N, end), this, next)`. `TB` is the input timebase, so dividing the seconds by it
    yields the value `setpts` expects.
    """

    pieces: list[tuple[int, str]] = []
    frame = 0
    tick = 0
    for count, cycle in profile.timing_segments:
        period = len(cycle)
        partial = "0"
        for offset in range(1, period):
            partial += f"+{sum(cycle[:offset])}*eq(mod(N-{frame},{period}),{offset})"
        pieces.append(
            (frame + count, f"{tick}+floor((N-{frame})/{period})*{sum(cycle)}+({partial})")
        )
        frame += count
        tick += sum(cycle[index % period] for index in range(count))
    expression = pieces[-1][1]
    for end, piece in reversed(pieces[:-1]):
        expression = f"if(lt(N,{end}),{piece},{expression})"
    return "setpts=" + f"({expression})/{SOURCE_TIME_BASE_DEN}/TB".replace(",", "\\,")


def _build_source_video(ffmpeg: Path, destination: Path, *, profile: SourceProfile) -> None:
    """Encode one real CFR H.264(+AAC) MP4 with the pinned, digest-verified ffmpeg.

    This is fixture construction, not the render path under test, so it shells out directly
    rather than through the product's Windows job-object session. Every frame is painted exactly
    per ``profile`` (``semantic_conformance_media.SOURCE_PROFILES``), and the embedded audio (when
    ``profile.carries_audio``) is the same module's burst/silence pattern, so the independent
    extractor is reading landmarks back out of the declared media, not out of a local restatement
    of it.

    GUARD: ``-video_track_timescale`` is ``SOURCE_TIME_BASE_DEN`` (12288), not the frame rate.
    Declaring a source's ``source_time_base`` at whatever value the container happened to get is
    exactly the vacuity a real measurement exists to prevent -- the corpus fixture states 1/12288
    for both video assets, and a real file's own measured stream timebase has to actually be that
    for the declared base to be true rather than restated. Leave ``-enc_time_base:v 1:24`` alone
    while doing this: it is the encoder's internal frame-rate assumption, a different knob from the
    container's track timescale, and changing it to match ``SOURCE_TIME_BASE_DEN`` too (confirmed
    empirically) silently turns a 24fps encode into a garbage 12288fps one -- always re-check
    ``ffprobe -show_entries stream=r_frame_rate`` reads 24/1 after touching either flag.
    """

    import subprocess  # noqa: S404 -- fixed argv, digest-verified binary, no shell

    raw_dir = destination.parent
    raw_video_path = raw_dir / (destination.stem + ".raw")
    frames = profile.frame_count
    raw_video_path.write_bytes(b"".join(_paint_frame(profile, index) for index in range(frames)))
    graph = "scale=out_color_matrix=bt709:in_range=full:out_range=tv,format=yuv420p,setsar=1"
    # The source's declared timing (`SourceProfile.pts_table`). A constant rate is encoded as
    # CFR at that rate; unequal intervals are stamped frame by frame with `setpts` and passed
    # through, so the container carries exactly the declared ticks and the product's probe
    # measures exactly the declared table. Either way the file is what the corpus base states.
    rate = profile.constant_rate()
    durations = profile.durations()
    # GUARD (measured, 2026-09-10): `settb` must precede `setpts`, or the stamped ticks are
    # rounded to the raw input's 1/24 grid and the file comes out CFR with no diagnostic; and the
    # raw input's `-framerate` is what the encoder stamps as the *last* packet's duration (every
    # other packet's is the gap to its successor), so it is set to the rate whose period is the
    # declared last duration -- 12288/384 = 32 for the VFR source -- or the probe measures a last
    # landmark the base does not declare. `-r` cannot be used here: ffmpeg refuses it with
    # passthrough.
    if rate is None:
        graph += ",settb=1/" + str(SOURCE_TIME_BASE_DEN) + "," + _vfr_setpts(profile)
        if SOURCE_TIME_BASE_DEN % durations[-1] != 0:
            raise RenderStageError("source_timing_unencodable")
        input_rate = SOURCE_TIME_BASE_DEN // durations[-1]
    else:
        input_rate = rate
    source_seconds = sum(durations) / SOURCE_TIME_BASE_DEN
    args = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{profile.width}x{profile.height}",
        "-framerate",
        str(input_rate),
        "-i",
        str(raw_video_path),
    ]
    raw_audio_path: Path | None = None
    if profile.carries_audio:
        sample_count = round(source_seconds * AUDIO_SAMPLE_RATE)
        raw_audio_path = raw_dir / (destination.stem + ".pcm")
        raw_audio_path.write_bytes(_build_audio_pcm(sample_count, profile.audio_kind))
        args += [
            "-f",
            "s16le",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-ac",
            "1",
            "-i",
            str(raw_audio_path),
        ]
    args += [
        "-vf",
        graph,
        "-fps_mode",
        "cfr" if rate is not None else "passthrough",
        "-enc_time_base:v",
        f"1:{rate}" if rate is not None else f"1:{SOURCE_TIME_BASE_DEN}",
        "-frames:v",
        str(frames),
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-bf",
        "0",
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
        "-x264-params",
        "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-video_track_timescale",
        str(SOURCE_TIME_BASE_DEN),
        "-movie_timescale",
        str(AUDIO_SAMPLE_RATE),
    ]
    args += (
        ["-c:a", "aac", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1"]
        if profile.carries_audio
        else ["-an"]
    )
    args += [str(destination)]
    completed = subprocess.run(  # noqa: S603 -- fixed argv, digest-verified binary, no shell
        args, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS, check=False
    )
    raw_video_path.unlink(missing_ok=True)
    if raw_audio_path is not None:
        raw_audio_path.unlink(missing_ok=True)
    if completed.returncode != 0 or not destination.is_file():
        raise RenderStageError("source_fixture_encode_failed")


@dataclass(slots=True)
class RenderRuntime:
    """Everything the render loop needs, built once and released once.

    The renderer and probe are pinned exactly once here rather than per row: a pin verifies the
    whole binary by digest, and ``PinnedRenderExecutable`` is designed to be borrowed by many
    sequential ``session.run`` calls (it is reference-counted for exactly that reuse), not
    re-verified for every one of a few hundred small renders.
    """

    ffmpeg_path: Path
    ffprobe_path: Path
    encoder: PinnedRenderExecutable
    probe: PinnedRenderExecutable
    fonts: PackagedFontManifest
    #: The stateless capture ingredients, reused to mint one fresh receipt per render (see
    #: ``_mint_row_receipt``). Never held as one long-lived receipt across the whole stage: a
    #: captured image source carries a fixed expiry (``ImageSourcePool``'s TTL, capped by that
    #: adapter at 900 seconds) timed from the moment it is captured, not from when it is last
    #: used, so one receipt captured at startup and reused for ~180 sequential real renders
    #: silently starts refusing every subsequent render as ``source_stale`` once that many
    #: seconds of real ffmpeg/ffprobe work have elapsed -- a real defect this stage found in
    #: itself, not in the product: the currentness check is doing exactly its job on a claim
    #: this stage kept alive far past the scope it was minted for. Minting fresh per render
    #: keeps every claim's remaining lifetime irrelevant to how long the whole stage runs.
    factory: RuntimeComfySourceFactory
    registry: ReferenceRegistry
    image_tensor: Any
    image_pool: ImageSourcePool
    adapter: QualifiedAVMediaAdapter
    #: Where each render's own registry roots its store, and where the verified body is written
    #: for the extractor; one directory per render, never shared between them.
    render_dir: Path
    base_wire: Mapping[str, Any]
    scratch_root: Path
    #: Which corpus base this runtime was built for, and the assets that base declares. A row is
    #: rendered by the runtime its own recipe names; see `BASE_VIDEO_ASSETS`.
    base: str = CORPUS_BASE
    video_assets: tuple[str, ...] = ()
    image_asset: str = IMAGE_ASSET

    def close(self) -> None:
        self.encoder.close()
        self.probe.close()
        self.image_pool.close()
        clear_authoring_media_preview_adapter(self.adapter)


def _load_fixture_document(base: str = CORPUS_BASE) -> dict[str, Any]:
    path = FIXTURES_DIR / BASE_FIXTURE_NAMES[base]
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _base_wire(
    document: Mapping[str, Any], claims: Mapping[str, AuthoringRenderSourceClaim]
) -> dict[str, Any]:
    """The accepted base composition: the corpus fixture's own shape, with real assets bound in.

    Reusing the fixture's clip geometry (durations, start frames, transforms) *and* its declared
    output canvas (320x180, 48 frames) rather than re-deriving either keeps every command
    payload's hardcoded frame offsets (a split at frame 12, a slide's second split at offset 35,
    and so on) meaningful -- they were declared against exactly this layout -- and keeps every
    ``output.*`` dimension-admission row exercising the dimension it names instead of a
    stage-local resize of the very composition under test.

    The fixture's own font asset entry (the placeholder ``font-main``) is left untouched here,
    deliberately: ``semantic_conformance_cases.normalize_base`` renames it to the real packaged
    font, and ``semantic_conformance_drive`` (the shared driver ``_build_case_snapshots`` below
    calls into) is what invokes ``apply_edits`` -- exactly once per row -- never here. Normalizing
    twice in two places is exactly the failure mode its guard comment warns against.
    """

    wire = cast(dict[str, Any], copy.deepcopy(document["snapshot"]))
    # The base's own asset order, with each synthetic source replaced by what was actually bound.
    # Taking the order from the fixture rather than restating it is what keeps every command
    # payload's asset ordinal meaningful -- including the timing source, which the accepted base
    # declares last precisely so that the rows written before it kept their ordinals.
    wire["assets"] = [
        asset if asset["kind"] == "font" else claims[asset["asset_id"]].asset.to_wire()
        for asset in wire["assets"]
    ]
    # This wire is never decoded directly -- every consumer copies it and normalizes it fresh
    # through `apply_edits`, so it deliberately carries no (and would carry a stale) fingerprint.
    return wire


def _build_runtime(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    renderer_fingerprint: str,
    probe_fingerprint: str,
    scratch_root: Path,
    base: str = CORPUS_BASE,
) -> RenderRuntime:
    document = _load_fixture_document(base)
    video_assets = BASE_VIDEO_ASSETS[base]
    image_asset = BASE_IMAGE_ASSET[base]

    sources_dir = scratch_root / "sources" / base
    sources_dir.mkdir(parents=True, exist_ok=True)
    for asset_id in video_assets:
        video_profile = profile_for(asset_id)
        if video_profile is None:
            raise RenderStageError("source_profile_undeclared")
        _build_source_video(ffmpeg_path, sources_dir / f"{asset_id}.mp4", profile=video_profile)

    adapter = QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        scratch_root=scratch_root / "preview-scratch",
        clock_ms=lambda: max(1, time.monotonic_ns() // 1_000_000),
    )
    publish_authoring_media_preview_adapter(adapter)

    image_pool = ImageSourcePool()
    video_factory = PathBackedComfyVideoFromFileV1Factory(
        type_authority=_TYPE_AUTHORITY,
        input_root_factory=lambda: sources_dir,
        max_source_bytes=64 * 1024 * 1024,
    )
    factory = RuntimeComfySourceFactory(image_pool=image_pool, video_factory=video_factory)
    registry: ReferenceRegistry = build_reference_registry(
        (
            ReferenceAsset(image_asset, MediaKind.IMAGE, AssetRole.REFERENCE, 1),
            *(
                ReferenceAsset(asset_id, MediaKind.VIDEO, AssetRole.REFERENCE, rank)
                for rank, asset_id in enumerate(video_assets, start=2)
            ),
        )
    )
    image_profile = profile_for(image_asset)
    if image_profile is None:
        raise RenderStageError("source_profile_undeclared")
    image_tensor = _build_image_tensor(image_profile)

    # One bootstrap receipt, minted and released immediately, only to read the deterministic
    # asset facts every row's wire needs (see `_base_wire`). Rendering never touches this receipt
    # or its claims: `_render_and_observe` mints and releases its own per call, for the reason
    # documented on `RenderRuntime.factory` above.
    bootstrap_receipt = factory.capture(
        exact_registry=registry,
        generation=1,
        sources=(
            (image_asset, MediaKind.IMAGE, image_tensor),
            *(
                (asset_id, MediaKind.VIDEO, _file_video(f"{asset_id}.mp4"))
                for asset_id in video_assets
            ),
        ),
    )
    try:
        bootstrap_claims = {
            asset_id: claim_render_source(bootstrap_receipt, asset_id)
            for asset_id in (image_asset, *video_assets)
        }
        base_wire = _base_wire(document, bootstrap_claims)
    finally:
        bootstrap_receipt.release()

    fonts = load_packaged_font_manifest()

    render_dir = scratch_root / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)

    pin_control = Control(120.0)
    encoder = pin_render_executable(ffmpeg_path, renderer_fingerprint, control=pin_control)
    probe = pin_render_executable(ffprobe_path, probe_fingerprint, control=pin_control)

    return RenderRuntime(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        encoder=encoder,
        probe=probe,
        fonts=fonts,
        factory=factory,
        registry=registry,
        image_tensor=image_tensor,
        image_pool=image_pool,
        adapter=adapter,
        render_dir=render_dir,
        base_wire=base_wire,
        scratch_root=scratch_root,
        base=base,
        video_assets=video_assets,
        image_asset=image_asset,
    )


def _mint_row_receipt(runtime: RenderRuntime) -> AuthoringSourceBindingReceipt:
    """Mint one fresh source-binding receipt, scoped to exactly one render.

    See the guard comment on ``RenderRuntime.factory``: a captured image source's lifetime is
    timed from capture, not from last use, so a claim must be minted right before the render that
    uses it and released right after, never held across other rows.
    """

    return runtime.factory.capture(
        exact_registry=runtime.registry,
        generation=1,
        sources=(
            (runtime.image_asset, MediaKind.IMAGE, runtime.image_tensor),
            *(
                (asset_id, MediaKind.VIDEO, _file_video(f"{asset_id}.mp4"))
                for asset_id in runtime.video_assets
            ),
        ),
    )


# ---------------------------------------------------------------------------------------------
# Composing one row's snapshot(s) from its recipe, via real timeline transactions and edits.
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CaseSnapshots:
    """The snapshot(s) a recipe resolves to: one for a property row, two for a command effect."""

    phases: tuple[tuple[str, PublicCompositionSnapshot], ...]


def _build_case_snapshots(base_wire: Mapping[str, Any], recipe: CaseRecipe) -> CaseSnapshots:
    """Build the snapshot(s) one recipe resolves to, from the shared corpus driver.

    ``run_setup``/``command_phases`` (``comfyui_h3_context.core.semantic_conformance_drive``) are
    the same driver ``scripts/nle_semantic_qualify.py`` and the report join use, so all three
    agree on what a row's setup and subject command actually do -- including calling
    ``apply_edits`` (and, through it, the font placeholder normalization) exactly once per row,
    and substituting ``$setup_cursor``/``$timeline_fingerprint``/``$stale_fingerprint`` tokens
    anywhere they appear in a payload, nested included, which this stage's own now-removed
    top-level-only substitution did not.
    """

    if recipe.command is None:
        setup = run_setup(recipe, base_wire)
        if setup.state is None:
            raise DriveError(setup.error or "the row reached no usable state")
        return CaseSnapshots(phases=(("single", setup.state.snapshot),))
    phases = command_phases(recipe, base_wire)
    return CaseSnapshots(phases=(("before", phases.before), ("after", phases.after)))


# ---------------------------------------------------------------------------------------------
# Rendering one snapshot through the accepted path, then observing it independently.
# ---------------------------------------------------------------------------------------------


#: See `PhaseResult.artifact_retrieval`; the join admits only this label.
VERIFIED_DOWNLOAD: Final = ACCEPTED_ARTIFACT_RETRIEVAL


@dataclass(slots=True)
class PhaseResult:
    phase: str
    status: str
    #: The fingerprint of the composition this phase actually rendered (or attempted to), set
    #: explicitly from the snapshot passed to ``_render_phase`` -- for a cache hit that is the
    #: same value as the cached result's own, but it is passed in rather than copied off the
    #: cached object, since the two are only guaranteed equal because the cache is keyed by this
    #: exact fingerprint, not because one was derived from the other.
    public_fingerprint: str
    blocked_code: str | None = None
    measured_frame_count: int | None = None
    measured_audio_streams: int | None = None
    observation: Mapping[str, Any] | None = None
    #: Honest label for how the observed bytes reached the independent extractor. `None` for a
    #: BLOCKED phase (nothing was retrieved). `VERIFIED_DOWNLOAD` for every OBSERVED phase: the
    #: bytes were obtained through the accepted M25-19 transport -- `AuthoringOutputRegistry`'s
    #: create / status / download path, the same registry, render service and native renderer
    #: `comfyui_authoring_output_runtime.build_authoring_output_runtime` assembles for the product
    #: -- and the download is the store's completely verified body (`RenderOutputStore.read_output`
    #: checks the whole artifact against its receipt before a byte is served). This stage then
    #: checks the served bytes against the receipt's own output fingerprint once more before
    #: decoding them, so the label is a measured fact and not a route name.
    #:
    #: GUARD: the workspace port the registry drives (`_CorpusOutputWorkspace`) serves the row's
    #: composition exactly as the corpus states it, and the corpus base carries a workspace handle
    #: minted the product's own way (`"authoring-" + secrets.token_hex(16)`, frozen once in
    #: `tests/fixtures/m25_20_semantic_corpus_base_v1.json`) so that `require_output_workspace`
    #: and `_read_currency` admit it. Never rewrite that handle, or any other identity field, in
    #: this stage's own wire to satisfy the transport: `workspace_handle` is folded into
    #: `public_snapshot_fingerprint`, so a stage-local rewrite would silently move every row's
    #: fingerprint away from the value the backend and browser stages compute from the identical
    #: fixture and break the cross-stage identity binding. Never fall back to decoding the staging
    #: file directly either -- that proves the encoder, not the transport, and the two are
    #: different claims the report has to keep apart.
    artifact_retrieval: str | None = None

    def as_wire(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "public_fingerprint": self.public_fingerprint,
            "blocked_code": self.blocked_code,
            "measured_frame_count": self.measured_frame_count,
            "measured_audio_streams": self.measured_audio_streams,
            "artifact_retrieval": self.artifact_retrieval,
            "observation": self.observation,
        }


def _alpha_targets(wire: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Which clips have an active transition ramp, where to sample them, and at which frames.

    Every value here is a literal field already on the clip (`start_frame`, `transition.
    duration_frames`, `opacity_bp`) or a canvas point `alpha_sample_point` chose for this exact
    wire -- this states where and when to look, and the clip's own declared opacity ceiling, never
    what alpha should measure there. `opacity_bp` is read back the same way `blend`/`start_frame`/
    `transition.duration_frames` already are elsewhere in this module: an input specification the
    render pipeline itself consumes, not a derived expectation.

    GUARD -- the measurability rule, stated precisely rather than as a list of exempt rows: a
    clip's alpha is recoverable by this stage at a frame if, and only if, the clip declares an
    *active* cross-dissolve transition somewhere in its own span (`transition.kind != "none"` and
    `transition.duration_frames > 0`) and the composition offers a point where that ramp moves the
    composite far enough to measure (`ALPHA_MEASURED_WHERE_THE_RAMP_IS_LOUDEST`). Such a clip's
    first included frame and the frame one ramp-length later are the only two states inside one
    render with a *known* relative position on the ramp (0 and 1), so the ratio between them and
    any other frame in the clip's span -- during the ramp or after it plateaus -- recovers the
    ramp's progress honestly. That ratio is progress *toward the clip's own steady pixel*, not an
    absolute alpha, because the clip's declared opacity cancels out of the ratio algebraically
    regardless of its value. Recovering the absolute value the contract states requires scaling
    that measured ratio by `opacity_bp`, which is why it travels with the target.

    A clip with no active transition anywhere in its span offers exactly one relative-alpha state
    for its entire lifetime -- there is no second, contrasting reference frame inside the same
    render to measure a ratio against at all -- so its alpha is unmeasurable by this stage at every
    one of its frames, full stop. Reporting one anyway would mean either reading `opacity_bp` back
    with no pixel evidence in the loop (the self-confirmation this stage exists to refuse) or
    independently reconstructing the pre-blend/effect reference colour to solve for alpha directly
    (a second, uncoordinated implementation of the compositing rules `semantic_conformance_expect`
    already owns). This is a structural condition on the clip, not a per-row exemption list: any
    clip built the same way, in any future row, falls on the same side of it.
    """

    # The targets themselves (point, reference frames, reported frames) are the oracle module's
    # `alpha_targets`, shared with the browser journey's prescription so both observers look at
    # the same pixels; this stage only shapes them for the extractor.
    return [
        {
            "label": target.label,
            "canvas_point": (target.canvas_x, target.canvas_y),
            "opacity_bp": target.opacity_bp,
            "base_frame": target.base_frame,
            "steady_frame": target.steady_frame,
            "sample_frames": target.sample_frames,
        }
        for target in alpha_targets(wire)
    ]


def _text_target(wire: Mapping[str, Any]) -> dict[str, Any] | None:
    """The declared text facts, plus which frame to look at for ink.

    Content/font/weight/style/align/fill are read back from the wire the way a case id is: input
    specification, not a rendering outcome. `sample_frames` names the title clip's own midpoint,
    the one frame this stage already knows the clip is active for, so the caller can independently
    measure whether ink actually appears there and where -- see
    ``nle_semantic_conformance._text_observation``.
    """

    for clip in wire.get("clips", ()):
        if not isinstance(clip, Mapping) or clip.get("enabled") is not True:
            continue
        text = clip.get("text")
        if not isinstance(text, Mapping):
            continue
        start = clip.get("start_frame")
        span = clip.get("duration_frames")
        if not isinstance(start, int) or not isinstance(span, int) or span <= 0:
            continue
        midpoint = start + span // 2
        return {
            "content": text.get("content", ""),
            "font_asset_id": text.get("font_asset_id", ""),
            "weight": text.get("weight", 0),
            "style": text.get("style", ""),
            "align": text.get("align", ""),
            "fill_rgba": text.get("fill_rgba"),
            "sample_frames": (midpoint,),
        }
    return None


def _audio_owner_windows(wire: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One output-sample window per primary clip that actually carries embedded audio.

    Mirrors the declared scheduling policy's own literal arithmetic -- which clips are on the
    primary track, which asset each uses, where each starts and for how long, the output sample
    rate -- entirely from wire fields, in timeline order. It states *where to look*, never what
    signal should be there: the actual onsets and silences are measured from decoded PCM by
    ``nle_semantic_conformance._scan_onsets``/``_measure_silences``, independent of this.
    """

    tracks = wire.get("tracks")
    output = wire.get("output")
    if not isinstance(tracks, Sequence) or not isinstance(output, Mapping):
        return []
    primary_track_ids = {
        track.get("track_id")
        for track in tracks
        if isinstance(track, Mapping) and track.get("kind") == "primary_video"
    }
    enabled_track_ids = {
        track.get("track_id")
        for track in tracks
        if isinstance(track, Mapping) and track.get("enabled") is True
    }
    assets_by_id = {
        asset.get("asset_id"): asset
        for asset in wire.get("assets", ())
        if isinstance(asset, Mapping)
    }
    frame_rate = output.get("frame_rate")
    sample_rate = output.get("sample_rate")
    if (
        not isinstance(frame_rate, Mapping)
        or not isinstance(sample_rate, int)
        or not isinstance(frame_rate.get("num"), int)
        or not isinstance(frame_rate.get("den"), int)
        or frame_rate["num"] <= 0
    ):
        return []
    samples_per_frame = sample_rate * frame_rate["den"] / frame_rate["num"]

    owners: list[Mapping[str, Any]] = []
    for clip in wire.get("clips", ()):
        if not isinstance(clip, Mapping) or clip.get("enabled") is not True:
            continue
        if clip.get("track_id") not in primary_track_ids:
            continue
        if clip.get("track_id") not in enabled_track_ids:
            continue
        asset = assets_by_id.get(clip.get("asset_id"))
        if asset is None or asset.get("embedded_audio") != "present_bound":
            continue
        span = clip.get("duration_frames")
        if not isinstance(span, int) or span <= 0:
            continue
        # A tone has no bursts to find: its sound is measured as window levels instead
        # (`audio_level_windows`), mirroring `semantic_conformance_expect._audio_clips`.
        profile = profile_for(str(clip.get("asset_id")))
        if profile is not None and profile.audio_kind == "tone":
            continue
        owners.append(clip)
    owners.sort(key=lambda clip: (clip.get("start_frame", 0), str(clip.get("clip_id"))))

    windows: list[dict[str, Any]] = []
    for clip in owners:
        start = int(clip["start_frame"])
        span = int(clip["duration_frames"])
        asset = assets_by_id[clip.get("asset_id")]
        # Where the owner's source sample 0 lands on the output: the clip's start minus its
        # source start frame's *time* (the landmark's pts, B-58), never its frame count times an
        # output frame -- the timing source's frames are not output frames long. This is also the
        # boundary the extractor measures a source-start burst's gap from (B-61).
        start_tick = source_start_tick(wire, clip)
        base = asset.get("source_time_base")
        if start_tick is None or not isinstance(base, Mapping):
            return []
        source_start_sample = Fraction(
            start_tick * int(base["num"]) * sample_rate, int(base["den"])
        )
        windows.append(
            {
                "start_sample": int(start * samples_per_frame),
                "end_sample": int((start + span) * samples_per_frame),
                "shift_samples": int(start * samples_per_frame - source_start_sample),
                "burst_starts": AUDIO_BURST_STARTS,
                "burst_samples": AUDIO_BURST_SAMPLES,
                # The declared codec's frame length, so the extractor measures the gap after a
                # burst inside its source's first frame from the boundary the expectation
                # declares (`semantic_conformance_expect.derive_silences`, B-45/B-61).
                "codec_frame_samples": CODEC_FRAME_SAMPLES.get(
                    str(output.get("audio_codec") or ""), 0
                ),
            }
        )
    return windows


def _level_windows(wire: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Where to measure a level, from the oracle module's own windows -- never what to find."""

    return [
        {
            "label": window.label,
            "start_sample": window.start_sample,
            "sample_count": window.sample_count,
        }
        for window in audio_level_windows(wire)
    ]


@dataclass(frozen=True, slots=True)
class Prescription:
    """Where one composition tells the observers to look, derived from the wire alone."""

    points: tuple[SamplePoint, ...]
    reads: tuple[IdentityRead, ...]
    targets: tuple[GeometryTarget, ...]
    alpha_targets: tuple[dict[str, Any], ...]
    #: M25-77: the windows whose audio level is measured (`audio_level_windows`), shaped for the
    #: extractor. A window is named by its label, its first output sample and its length.
    level_windows: tuple[dict[str, Any], ...] = ()

    @classmethod
    def for_wire(cls, wire: Mapping[str, Any]) -> Prescription:
        return cls(
            points=tuple(patch_sample_points(wire)),
            reads=tuple(identity_reads(wire)),
            targets=tuple(geometry_targets(wire)),
            alpha_targets=tuple(_alpha_targets(wire)),
            level_windows=tuple(_level_windows(wire)),
        )

    def joined_with(self, other: Prescription) -> Prescription:
        """This prescription plus every point of `other` this one does not already name.

        Keyed by what the observation is keyed by -- a patch label, an identity frame, a geometry
        or alpha clip label -- so that where both phases name the same thing this phase's own
        point stands and the companion's is not measured twice under one name.
        """

        labels = {point.label for point in self.points}
        frames = {read.output_frame for read in self.reads}
        located = {target.clip_id for target in self.targets}
        # An alpha target the other phase also names keeps this phase's own point and ramp ends
        # -- the ratio is only meaningful against this artifact's own ramp -- and gains the other
        # phase's sample frames, so that the alpha the other expectation states at a frame this
        # phase would not otherwise visit is still a measurement of this artifact there.
        other_frames = {
            str(target["label"]): tuple(target.get("sample_frames", ()))
            for target in other.alpha_targets
        }
        alpha_targets = tuple(
            {
                **target,
                "sample_frames": tuple(
                    sorted(
                        set(target.get("sample_frames", ()))
                        | set(other_frames.get(str(target["label"]), ()))
                    )
                ),
            }
            for target in self.alpha_targets
        )
        ramped = {str(target["label"]) for target in self.alpha_targets}
        # A level window the other phase names is measured here too, so that the other
        # expectation's level is a measurement of this artifact at the same samples.
        windowed = {str(window["label"]) for window in self.level_windows}
        return Prescription(
            points=self.points + tuple(p for p in other.points if p.label not in labels),
            reads=self.reads + tuple(r for r in other.reads if r.output_frame not in frames),
            targets=self.targets + tuple(t for t in other.targets if t.clip_id not in located),
            alpha_targets=alpha_targets
            + tuple(t for t in other.alpha_targets if str(t["label"]) not in ramped),
            level_windows=self.level_windows
            + tuple(w for w in other.level_windows if str(w["label"]) not in windowed),
        )


def _companion_prescriptions(
    snapshots: Sequence[tuple[str, PublicCompositionSnapshot]],
) -> dict[str, Prescription]:
    """Each phase's prescription joined with every other phase's, keyed by phase name.

    GUARD: a command row is proved by its "after" artifact *failing* its "before" expectation on
    a measurement, and the two phases prescribe different points -- a trimmed primary states no
    identity at the frames it vacated, a moved overlay states its ends a frame later. An "after"
    artifact observed only at its own points would then differ from the before expectation by
    absence alone, which a stage that rendered the same picture twice would also show; the join
    (`nle_semantic_report._command_row_phases`) refuses that as unmeasured. So every phase of a
    command row is observed at the union of the phases' points. A single-phase row is observed at
    its own points only: there is nothing to hold it to but its own expectation.
    """

    own = {name: Prescription.for_wire(snapshot.to_wire()) for name, snapshot in snapshots}
    joined: dict[str, Prescription] = {}
    for name, prescription in own.items():
        for other_name, other in own.items():
            if other_name != name:
                prescription = prescription.joined_with(other)
        joined[name] = prescription
    return joined


class _CorpusOutputWorkspace:
    """The M25-19 `OutputWorkspace` port over exactly one corpus composition.

    The registry reads the workspace's reference revision and its current timeline snapshot to
    decide whether a request is current, and asks the workspace to bind the render plan. Here the
    "workspace" is the row's own composition, already driven through the real decoder and command
    engine by the shared driver, with the row's own freshly minted source claims; nothing is
    derived from a render. One instance serves one render and is discarded with its registry.
    """

    def __init__(
        self,
        history: PreparedAuthoringHistory,
        snapshot: PublicCompositionSnapshot,
        deadline: float,
    ) -> None:
        self._history = history
        self._snapshot = snapshot
        self._deadline = deadline

    def dispatch(self, action: dict[str, object]) -> AuthoringDispatchResult:
        name = action.get("action")
        payload = action.get("payload")
        if not isinstance(payload, Mapping) or (
            payload.get("workspace_handle") != self._snapshot.workspace_handle
        ):
            raise AuthoringWorkbenchError(404, "workspace_unavailable")
        if name == "read_projection":
            return AuthoringDispatchResult(
                200, {"reference": {"revision": self._snapshot.workspace_revision}}
            )
        if name == "read_timeline_history":
            return AuthoringDispatchResult(200, {"snapshot": self._snapshot.to_wire()})
        raise AuthoringWorkbenchError(404, "unsupported_action")

    def prepare_render_plan(self, handle: str) -> BoundAuthoringRenderPlan:
        if handle != self._snapshot.workspace_handle:
            raise AuthoringWorkbenchError(404, "workspace_unavailable")
        return prepare_bound_render_plan(
            self._history, self._snapshot, lambda: True, deadline=self._deadline
        )


_TERMINAL_PHASES: Final = frozenset({"succeeded", "failed", "cancelled"})


def _retrieve_verified_output(
    runtime: RenderRuntime,
    history: PreparedAuthoringHistory,
    snapshot: PublicCompositionSnapshot,
    control: Control,
) -> tuple[bytes, Mapping[str, Any]]:
    """Render through the accepted M25-19 transport and return the verified body and its summary.

    Exactly the product's assembly (`build_authoring_output_runtime`): a `NativeAuthoringRenderer`
    on the pinned pair, an `AuthoringRenderService` on a `RenderOutputStore`, and an
    `AuthoringOutputRegistry` driven through create, status and download. One registry per
    render: the registry retains every entry until its TTL and admits thirty-two, and a sweep
    renders a few hundred.
    """

    return _retrieve_verified_output_via(
        runtime, _CorpusOutputWorkspace(history, snapshot, control.deadline), snapshot, control
    )


def _retrieve_verified_output_via(
    runtime: RenderRuntime,
    workspace: object,
    snapshot: PublicCompositionSnapshot,
    control: Control,
) -> tuple[bytes, Mapping[str, Any]]:
    """The transport above over any `OutputWorkspace` port.

    The corpus rows hand it `_CorpusOutputWorkspace`; the two import rows hand it the real
    `AuthoringWorkspaceRegistry` that performed the M25-29 import, so the render plan binds the
    imported source through the product's own lease rather than through a corpus claim (B-65:
    the receipt -> asset -> render plan -> verified output chain has to be the product's).
    """

    job_root = runtime.render_dir / ("job-" + secrets.token_hex(8))
    job_root.mkdir(parents=True, exist_ok=False)
    store = RenderOutputStore(job_root)
    service = AuthoringRenderService(
        store,
        backend=NativeAuthoringRenderer(
            renderer_path=runtime.ffmpeg_path, probe_path=runtime.ffprobe_path
        ),
    )
    registry = AuthoringOutputRegistry(
        workspace=workspace,  # type: ignore[arg-type]
        service=service,
        store=store,
    )
    try:
        request = AuthoringOutputCreate(
            schema=OUTPUT_CREATE_SCHEMA,
            workspace_handle=snapshot.workspace_handle,
            workspace_revision=snapshot.workspace_revision,
            timeline_revision=snapshot.timeline_revision,
            snapshot_fingerprint=snapshot.public_fingerprint,
            output_profile_id=OUTPUT_PROFILE_ID,
            idempotency_key="nle-semantic-" + secrets.token_hex(8),
        )
        status = registry.create(request)
        job_handle = str(status["job_handle"])
        while status["phase"] not in _TERMINAL_PHASES:
            if time.monotonic() >= control.deadline:
                registry.cancel(job_handle, snapshot.workspace_handle)
                raise RenderStageError("deadline")
            time.sleep(0.2)
            status = registry.status(job_handle, snapshot.workspace_handle)
        if status["phase"] != "succeeded":
            raise RenderStageError(f"output_{status.get('failure') or status['phase']}")
        if status.get("availability") != "available" or status.get("output_handle") is None:
            raise RenderStageError(f"output_{status.get('availability') or 'unavailable'}")
        summary = status.get("output")
        if not isinstance(summary, Mapping) or summary.get("verified") is not True:
            raise RenderStageError("output_unverified")
        lease = registry.open_download(str(status["output_handle"]), snapshot.workspace_handle)
        with lease:
            body = b"".join(bytes(chunk) for chunk in lease.chunks())
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != summary.get("output_fingerprint") or len(body) != summary.get("byte_length"):
            raise RenderStageError("output_fingerprint_mismatch")
        return body, dict(summary)
    finally:
        registry.close()


def _render_and_observe(
    runtime: RenderRuntime,
    snapshot: PublicCompositionSnapshot,
    *,
    timeout_seconds: float,
    prescription: Prescription | None = None,
) -> tuple[Mapping[str, Any], int, int]:
    """Run the accepted path end to end and return the independently observed facts.

    A fresh receipt and claim set are minted here, bound to this one render, and released when it
    finishes -- never the shared, long-lived receipt the earlier version of this stage built once
    and reused for every row. See the guard comment on ``RenderRuntime.factory`` for why that was
    a real bug this stage found in itself: a captured image source's currentness window is timed
    from capture, not from last use, so a receipt reused across ~180 sequential renders eventually
    stops being current partway through a real run, regardless of the composition each row
    renders. Minting fresh here is the same deterministic probe against the same unchanged source
    files, just requested at the scope the currentness check assumes.
    """

    control = Control(timeout_seconds)
    # Where a colour comparison must sample, derived from the exact composition this call is
    # about to render -- never a render receipt, plan or graph. This is the specification the
    # corpus declares (crop/transform/track order/effect/blend), the same input the render
    # pipeline itself is handed below; it is not a fact the renderer produced. The independent
    # extractor still measures the actual decoded pixel at each point rather than being told what
    # it should find there.
    wire = snapshot.to_wire()
    if prescription is None:
        prescription = Prescription.for_wire(wire)
    points = prescription.points
    reads = prescription.reads
    targets = prescription.targets
    alpha_targets = list(prescription.alpha_targets)
    text_target = _text_target(wire)
    audio_owner_windows = _audio_owner_windows(wire)
    receipt = _mint_row_receipt(runtime)
    try:
        row_claims = {
            asset_id: claim_render_source(receipt, asset_id)
            for asset_id in (runtime.image_asset, *runtime.video_assets)
        }
        history = PreparedAuthoringHistory(
            snapshot, receipt.generation, tuple(row_claims.values()), runtime.fonts
        )
        body, summary = _retrieve_verified_output(runtime, history, snapshot, control)
        artifact = runtime.render_dir / ("verified-" + secrets.token_hex(8) + ".mp4")
        artifact.write_bytes(body)
        try:
            remaining = max(1.0, control.deadline - time.monotonic())
            observation = extractor.observe_artifact(
                runtime.ffmpeg_path,
                runtime.ffprobe_path,
                artifact,
                deadline_seconds=min(remaining, DECODE_TIMEOUT_SECONDS),
                identity_reads=reads,
                patch_points=points,
                geometry_targets=targets,
                alpha_targets=alpha_targets,
                text_target=text_target,
                audio_owner_windows=audio_owner_windows,
                audio_level_windows=prescription.level_windows,
            )
        finally:
            artifact.unlink(missing_ok=True)
        return observation, int(summary["frame_count"]), int(summary["audio_streams"])
    finally:
        receipt.release()


def _render_phase(
    runtime: RenderRuntime,
    phase: str,
    snapshot: PublicCompositionSnapshot,
    *,
    timeout_seconds: float,
    cache: dict[str, PhaseResult],
    prescription: Prescription | None = None,
    companions: Sequence[str] = (),
) -> PhaseResult:
    fingerprint = snapshot.public_fingerprint
    # GUARD: the cache is keyed by the composition *and* by where it was observed. A command
    # row's phase is observed at the union of its phases' points (`_companion_prescriptions`),
    # which is a function of the companion phases' compositions, so the base composition
    # observed for one command row carries points another row's base does not; keying by
    # composition alone would hand a row an observation that lacks the points its own
    # before/after proof needs, which the join then blocks as unmeasured.
    cache_key = "+".join((fingerprint, *sorted(companions)))
    cached = cache.get(cache_key)
    if cached is not None:
        return PhaseResult(
            phase=phase,
            status=cached.status,
            public_fingerprint=fingerprint,
            blocked_code=cached.blocked_code,
            measured_frame_count=cached.measured_frame_count,
            measured_audio_streams=cached.measured_audio_streams,
            observation=cached.observation,
            artifact_retrieval=cached.artifact_retrieval,
        )
    try:
        observation, frame_count, audio_streams = _render_and_observe(
            runtime, snapshot, timeout_seconds=timeout_seconds, prescription=prescription
        )
        result = PhaseResult(
            phase=phase,
            status="OBSERVED",
            public_fingerprint=fingerprint,
            measured_frame_count=frame_count,
            measured_audio_streams=audio_streams,
            observation=observation,
            artifact_retrieval=VERIFIED_DOWNLOAD,
        )
    except _PHASE_EXCEPTIONS as exc:
        result = PhaseResult(
            phase=phase,
            status="BLOCKED",
            public_fingerprint=fingerprint,
            blocked_code=_phase_code(exc),
        )
    cache[cache_key] = result
    return result


#: The import rows start from an empty V2 authoring state. The product's first insert is a
#: 24-frame generated source, so the materialized V1 render snapshot is content-sized rather than
#: inheriting the old fixed-duration 3,600-frame authoring seed.
IMPORT_ROW_TIMEOUT_SECONDS: Final = 300.0
IMPORT_DECODE_TIMEOUT_SECONDS: Final = 120.0
_IMPORT_ROW_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *_PHASE_EXCEPTIONS,
    import_scenario.ImportScenarioError,
)


def _observe_import_composition(
    runtime: RenderRuntime,
    scenario: import_scenario.ImportScenario,
    wire: Mapping[str, Any],
    asset_id: str,
) -> tuple[Mapping[str, Any], int, int]:
    """Render the post-insertion composition through the real registry and observe it."""

    control = Control(IMPORT_ROW_TIMEOUT_SECONDS)
    snapshot = decode_public_snapshot(dict(wire))
    # The prescription is derived from the composition the product produced (the render's
    # input), with the imported asset read as the profile the product's own probe confirmed.
    with imported_source(asset_id):
        prescription = Prescription.for_wire(wire)
        text_target = _text_target(wire)
        audio_owner_windows = _audio_owner_windows(wire)
    body, summary = _retrieve_verified_output_via(runtime, scenario.authoring, snapshot, control)
    artifact = runtime.render_dir / ("verified-import-" + secrets.token_hex(8) + ".mp4")
    artifact.write_bytes(body)
    try:
        remaining = max(1.0, control.deadline - time.monotonic())
        observation = extractor.observe_artifact(
            runtime.ffmpeg_path,
            runtime.ffprobe_path,
            artifact,
            deadline_seconds=min(remaining, IMPORT_DECODE_TIMEOUT_SECONDS),
            identity_reads=prescription.reads,
            patch_points=prescription.points,
            geometry_targets=prescription.targets,
            alpha_targets=list(prescription.alpha_targets),
            text_target=text_target,
            audio_owner_windows=audio_owner_windows,
            audio_level_windows=prescription.level_windows,
        )
    finally:
        artifact.unlink(missing_ok=True)
    return observation, int(summary["frame_count"]), int(summary["audio_streams"])


def run_import_row(runtime: RenderRuntime, recipe: CaseRecipe) -> dict[str, Any]:
    """One `import_integration` row: the real chain, then the final artifact observed (B-65).

    Import through the real M25-29 service, insert the minted asset through the real history,
    render that composition through the accepted transport with the real registry binding the
    imported source, observe the artifact independently, then undo and redo through the real
    history. Every identity the chain minted is recorded so the join can hold the browser
    journey to the same composition, and the composition itself is recorded because it is the
    render's input (the product's timeline after the insertion), never the corpus base.
    """

    gesture = recipe.case_id.rsplit(".", 1)[-1]
    row: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "case_id": recipe.case_id,
        "case_class": recipe.case_class,
        "observation": recipe.observation,
        "status": "BLOCKED",
        "blocked_code": None,
        "phases": [],
        "import": {},
    }
    scenario: import_scenario.ImportScenario | None = None
    try:
        scenario = import_scenario.build_scenario(
            runtime.ffmpeg_path,
            runtime.ffprobe_path,
            root=runtime.scratch_root / f"import-{gesture}",
        )
        steps = import_scenario.import_and_insert(scenario, gesture=gesture)
        facts = steps.facts()
        wire = import_scenario.render_snapshot(steps.post_insert)
        row["import"] = {**facts, "composition": wire}
        if not imported_source_landmarks_match(steps.imported_asset):
            # The product measured a frame table that is not the imported profile's: the
            # identity chain is broken, and the row is judged on nothing rather than on a
            # borrowed profile.
            raise RenderStageError("imported_source_identity")
        observation, frame_count, audio_streams = _observe_import_composition(
            runtime, scenario, wire, steps.imported_asset_id
        )
        phase = PhaseResult(
            phase="post_insert",
            status="OBSERVED",
            public_fingerprint=str(wire["public_fingerprint"]),
            measured_frame_count=frame_count,
            measured_audio_streams=audio_streams,
            observation=observation,
            artifact_retrieval=VERIFIED_DOWNLOAD,
        )
        history = import_scenario.undo_and_redo(scenario, steps)
        # `all_facts` refuses a record that lacks any identity the join will require, so a
        # missing fact fails here, in the stage that owns it, not as a BLOCKED row downstream.
        row["import"] = {**import_scenario.all_facts(steps, history), "composition": wire}
        row["phases"] = [phase.as_wire()]
        row["status"] = "OBSERVED"
    except _IMPORT_ROW_EXCEPTIONS as exc:
        code = (
            exc.code if isinstance(exc, import_scenario.ImportScenarioError) else _phase_code(exc)
        )
        row["status"] = "BLOCKED"
        row["blocked_code"] = code
    finally:
        if scenario is not None:
            scenario.close()
    return row


def run_rendering_stage(
    runtime: RenderRuntime,
    recipes: Sequence[CaseRecipe],
    *,
    timeout_seconds: float,
) -> Iterator[dict[str, Any]]:
    cache: dict[str, PhaseResult] = {}
    for recipe in recipes:
        if recipe.observation == "import_integration":
            yield run_import_row(runtime, recipe)
            continue
        try:
            snapshots = _build_case_snapshots(runtime.base_wire, recipe)
        except _PHASE_EXCEPTIONS as exc:
            yield {
                "schema": REPORT_SCHEMA,
                "case_id": recipe.case_id,
                "case_class": recipe.case_class,
                "observation": recipe.observation,
                "status": "BLOCKED",
                "blocked_code": _phase_code(exc),
                "phases": [],
            }
            continue
        prescriptions = (
            _companion_prescriptions(snapshots.phases) if len(snapshots.phases) > 1 else {}
        )
        phase_results = [
            _render_phase(
                runtime,
                phase,
                snap,
                timeout_seconds=timeout_seconds,
                cache=cache,
                prescription=prescriptions.get(phase),
                companions=[
                    other.public_fingerprint
                    for other_phase, other in snapshots.phases
                    if other_phase != phase
                ],
            )
            for phase, snap in snapshots.phases
        ]
        overall = (
            "OBSERVED" if all(item.status == "OBSERVED" for item in phase_results) else "BLOCKED"
        )
        yield {
            "schema": REPORT_SCHEMA,
            "case_id": recipe.case_id,
            "case_class": recipe.case_class,
            "observation": recipe.observation,
            "status": overall,
            "blocked_code": None if overall == "OBSERVED" else _first_blocked_code(phase_results),
            "phases": [item.as_wire() for item in phase_results],
        }


def _first_blocked_code(phases: Sequence[PhaseResult]) -> str | None:
    for item in phases:
        if item.status == "BLOCKED":
            return item.blocked_code
    return None


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ffmpeg", type=Path, default=None, help="path to the pinned FFmpeg binary"
    )
    parser.add_argument(
        "--ffprobe", type=Path, default=None, help="path to the pinned ffprobe binary"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / ".tmp" / "nle_semantic_render_report.jsonl",
        help="where to write one JSON record per row (JSON Lines)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="render at most this many rows (for a bounded run)"
    )
    parser.add_argument(
        "--case-id", action="append", default=None, help="render only these case ids (repeatable)"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=90.0,
        help="per-phase render+observe deadline",
    )
    args = parser.parse_args(argv)

    ffmpeg_value = args.ffmpeg or os.environ.get("H3_CONTEXT_AUTHORIZED_FFMPEG_PATH")
    ffprobe_value = args.ffprobe or os.environ.get("H3_CONTEXT_AUTHORIZED_FFPROBE_PATH")
    if not ffmpeg_value or not ffprobe_value:
        print(
            "BLOCKED: exact authorized media tool paths were not explicitly supplied",
            file=sys.stderr,
        )
        return 2
    try:
        ffprobe_path = extractor.resolve_binary(Path(ffprobe_value).resolve(strict=True), "probe")
        ffmpeg_path = extractor.resolve_binary(Path(ffmpeg_value).resolve(strict=True), "renderer")
        qualification = json.loads(extractor.QUALIFICATION_PATH.read_text(encoding="utf-8"))
        renderer_fingerprint = cast(str, qualification["renderer"])
        probe_fingerprint = cast(str, qualification["probe"])
    except (extractor.QualificationError, OSError, KeyError, ValueError) as exc:
        print(f"BLOCKED: {_phase_code(exc)}", file=sys.stderr)
        return 2

    book: RecipeBook = build_recipes()
    recipes = list(book.rendering())
    if args.case_id:
        wanted = set(args.case_id)
        recipes = [item for item in recipes if item.case_id in wanted]
    if args.limit is not None:
        recipes = recipes[: args.limit]

    # GUARD (B-68): keep this name short. The store opens its files through `CreateFileW`
    # without the extended-length prefix, so a staging path longer than 260 characters fails
    # with `store_write_failed` on a host whose `LongPathsEnabled` is off; the product part of
    # the path below the scratch root is ~150 characters, and a worktree adds its own depth.
    scratch_root = ROOT / ".tmp" / f"nsr-{uuid.uuid4().hex[:12]}"
    scratch_root.mkdir(parents=True, exist_ok=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # One runtime per base, built and torn down in turn: a base owns real source files, a
    # reference registry at the product's own three-video ceiling, and a published preview
    # adapter, none of which two bases may hold at once. Rows keep their corpus order inside a
    # base, and a base with no selected row is never built.
    ordered_bases = [name for name in BASE_NAMES if any(recipe.base == name for recipe in recipes)]
    observed = 0
    blocked = 0
    try:
        with args.out.open("w", encoding="utf-8") as handle:
            for base_name in ordered_bases:
                selected = [recipe for recipe in recipes if recipe.base == base_name]
                runtime = _build_runtime(
                    ffmpeg_path,
                    ffprobe_path,
                    renderer_fingerprint,
                    probe_fingerprint,
                    scratch_root,
                    base_name,
                )
                try:
                    # One record per base, before its rows: the exact base wire those rows'
                    # snapshots are built from. Rendering substitutes real bound assets into the
                    # fixture's own shape (see `_base_wire`), so this composition -- and every
                    # public fingerprint derived from it -- is not the one in
                    # `tests/fixtures/m25_10_composition_contract_v1.json`. A join deriving a
                    # row's expectation from that fixture, or from the other base's record, would
                    # be comparing an artifact against a composition it was never rendered from.
                    header = {
                        "schema": REPORT_SCHEMA,
                        "record": "base_wire",
                        "base": base_name,
                        "base_wire": runtime.base_wire,
                    }
                    handle.write(json.dumps(header, ensure_ascii=False) + "\n")
                    handle.flush()
                    for row in run_rendering_stage(
                        runtime, selected, timeout_seconds=args.timeout_seconds
                    ):
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                        handle.flush()
                        if row["status"] == "OBSERVED":
                            observed += 1
                        else:
                            blocked += 1
                        print(
                            json.dumps({"case_id": row["case_id"], "status": row["status"]}),
                            flush=True,
                        )
                finally:
                    runtime.close()
    finally:
        _cleanup_scratch_root(scratch_root)

    print(
        json.dumps(
            {
                "rows_attempted": len(recipes),
                "rows_observed": observed,
                "rows_blocked": blocked,
                "report_path": str(args.out),
            }
        )
    )
    return 0


def _cleanup_scratch_root(scratch_root: Path) -> None:
    import shutil

    if scratch_root.is_relative_to(ROOT / ".tmp") and scratch_root.exists():
        shutil.rmtree(scratch_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
