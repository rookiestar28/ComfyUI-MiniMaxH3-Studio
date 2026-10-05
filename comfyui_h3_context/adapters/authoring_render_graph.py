"""Closed lowering of already-resolved authoring frames, never a command/history resolver."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from ..core.authoring_render_jobs import render_plan_fingerprint
from ..core.canonical import canonical_fingerprint
from ..core.clip_audio import SAMPLES_PER_FRAME, clip_audio_run
from ..core.composition_contract import ResolvedLayer
from ..core.render_planner import PlanClipAudio, RenderPlanV1
from .authoring_render_executor import PreparedRenderAssets


class RenderGraphError(RuntimeError):
    def __init__(self, code: str = "plan_mismatch") -> None:
        self.code = code if code in {"plan_mismatch", "resource_limit"} else "plan_mismatch"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class RenderLayerRun:
    layer: ResolvedLayer
    start: int
    end: int
    source_frames: tuple[int | None, ...]
    source_pts: tuple[int | None, ...]
    dissolve_frames: int


@dataclass(frozen=True, slots=True)
class RenderAudioRun:
    start: int
    end: int
    asset_id: str | None
    clip_id: str | None
    source_start: int


@dataclass(frozen=True, slots=True, repr=False)
class RenderSchedule:
    frame_count: int
    layers: tuple[RenderLayerRun, ...]
    audio: tuple[RenderAudioRun, ...]
    emits_audio_stream: bool


def _constant(layer: ResolvedLayer) -> ResolvedLayer:
    return replace(
        layer,
        source_frame=None,
        source_pts=None,
        transition_elapsed_frames=None,
        operation_ids=tuple(value for value in layer.operation_ids if value != "CrossDissolveV1"),
    )


def compile_render_schedule(plan: RenderPlanV1) -> RenderSchedule:
    if type(plan) is not RenderPlanV1 or not 1 <= plan.output_profile.duration_frames <= 3600:
        raise RenderGraphError()
    grouped: dict[str, list[tuple[int, ResolvedLayer]]] = {}
    precedes: dict[str, set[str]] = {}
    audio: list[RenderAudioRun] = []
    expected = 0
    for chunk in plan.resolved_operations:
        if (
            chunk.start_frame != expected
            or not chunk.frames
            or chunk.chunk_fingerprint != canonical_fingerprint(chunk.fingerprint_material())
        ):
            raise RenderGraphError()
        for frame in chunk.frames:
            if frame.frame != expected:
                raise RenderGraphError()
            expected += 1
            if len({layer.clip_id for layer in frame.layers}) != len(frame.layers):
                raise RenderGraphError()
            previous: str | None = None
            for layer in frame.layers:
                grouped.setdefault(layer.clip_id, []).append((frame.frame, layer))
                precedes.setdefault(layer.clip_id, set())
                if previous is not None:
                    precedes[layer.clip_id].add(previous)
                previous = layer.clip_id
            span = frame.audio_span
            if span is None:
                run = RenderAudioRun(frame.frame * 2000, (frame.frame + 1) * 2000, None, None, 0)
            else:
                if (
                    span.output_start_sample != frame.frame * 2000
                    or span.output_end_sample != (frame.frame + 1) * 2000
                    or span.source_start_sample < 0
                    or span.source_end_sample != span.source_start_sample + 2000
                ):
                    raise RenderGraphError()
                run = RenderAudioRun(
                    span.output_start_sample,
                    span.output_end_sample,
                    span.asset_id,
                    span.clip_id,
                    span.source_start_sample,
                )
            if audio and (
                audio[-1].end == run.start
                and (audio[-1].asset_id, audio[-1].clip_id) == (run.asset_id, run.clip_id)
                and (
                    run.asset_id is None
                    or audio[-1].source_start + audio[-1].end - audio[-1].start == run.source_start
                )
            ):
                audio[-1] = replace(audio[-1], end=run.end)
            else:
                audio.append(run)
    if expected != plan.output_profile.duration_frames or len(grouped) > 128:
        raise RenderGraphError()
    emits = any(run.asset_id is not None for run in audio)
    if emits is not plan.emits_audio_stream:
        raise RenderGraphError()
    # A clip first appearing later may be BELOW an existing clip. First-seen order would
    # invert that overlap; preserve the actual per-frame stack's partial order instead.
    ordered: list[str] = []
    while precedes:
        ready = sorted(key for key, before in precedes.items() if not before)
        if not ready:
            raise RenderGraphError()
        for key in ready:
            ordered.append(key)
            del precedes[key]
        for before in precedes.values():
            before.difference_update(ready)
    layers: list[RenderLayerRun] = []
    for key in ordered:
        rows = grouped[key]
        start, first = rows[0]
        constant = _constant(first)
        elapsed: list[int] = []
        prior_source = -1
        prior_pts = -1
        for offset, (position, layer) in enumerate(rows):
            if position != start + offset or _constant(layer) != constant:
                raise RenderGraphError()
            if layer.transition_elapsed_frames is not None:
                if layer.transition_elapsed_frames != offset or offset != len(elapsed):
                    raise RenderGraphError()
                elapsed.append(offset)
            if layer.source_frame is not None:
                if (
                    layer.source_pts is None
                    or layer.source_frame < prior_source
                    or layer.source_pts < prior_pts
                ):
                    raise RenderGraphError()
                prior_source, prior_pts = layer.source_frame, layer.source_pts
            elif layer.source_pts is not None:
                raise RenderGraphError()
        layers.append(
            RenderLayerRun(
                first,
                start,
                start + len(rows),
                tuple(row.source_frame for _, row in rows),
                tuple(row.source_pts for _, row in rows),
                len(elapsed),
            )
        )
    return RenderSchedule(expected, tuple(layers), tuple(audio), emits)


@dataclass(frozen=True, slots=True, repr=False)
class RenderGraphProgram:
    arguments: tuple[str, ...]
    filter_graph: str


def _decimal(value: float) -> str:
    if not math.isfinite(value):
        raise RenderGraphError()
    return format(value, ".12g")


def _clip_audio_filters(entry: PlanClipAudio, run: RenderAudioRun) -> str:
    """The filters that give a clip's audio adjustments over one of its audio runs."""

    origin = entry.start_frame * SAMPLES_PER_FRAME
    try:
        applied = clip_audio_run(
            entry.audio, entry.duration_frames, run.start - origin, run.end - origin
        )
    except ValueError as exc:
        # A run of the clip outside the clip: the plan does not describe one composition.
        raise RenderGraphError() from exc
    chain = ""
    if applied.volume is not None:
        chain += f",volume={_decimal(applied.volume)}"
    if applied.fade_in is not None:
        samples, silence = applied.fade_in
        chain += f",afade=t=in:ss=0:ns={samples}:silence={_decimal(silence)}"
    if applied.fade_out is not None:
        start, samples, unity = applied.fade_out
        chain += f",afade=t=out:ss={start}:ns={samples}"
        if unity != 1.0:
            # The run resumes inside the ramp and continues it from the level reached.
            chain += f":unity={_decimal(unity)}"
    return chain


def _rgba(value: tuple[int, int, int, int]) -> str:
    return "0x" + "".join(f"{channel:02x}" for channel in value)


def build_render_graph(*, plan: RenderPlanV1, prepared: PreparedRenderAssets) -> RenderGraphProgram:
    """Trusted generated graph only. The executor separately pins every private input."""
    if type(
        prepared
    ) is not PreparedRenderAssets or prepared.plan_fingerprint != render_plan_fingerprint(plan):
        raise RenderGraphError()
    schedule = compile_render_schedule(plan)
    width, height = plan.output_profile.width, plan.output_profile.height
    count = schedule.frame_count
    media = {row.asset_id: row for row in prepared.media}
    texts = {row.clip_id: row for row in prepared.texts}
    for path in (
        *(row.path for row in prepared.media),
        *(row.text_path for row in prepared.texts),
        *(row.font_path for row in prepared.texts),
        *(path for row in prepared.texts for path, _ in row.lines if path is not None),
    ):
        if (
            path.parent != prepared._stage.output_path.parent
            or re.fullmatch(r"[0-9a-f]{32}\.(bin|mp4|txt)", path.name) is None
        ):
            raise RenderGraphError()
    args = [
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-xerror",
        "-n",
        "-filter_threads",
        "2",
        "-filter_complex_threads",
        "2",
        "-max_alloc",
        "67108864",
    ]
    inputs: dict[str, int] = {}
    for row in prepared.media:
        if row.asset_id in inputs:
            raise RenderGraphError()
        inputs[row.asset_id] = len(inputs)
        if row.origin == "runtime_image":
            args += [
                "-stream_loop",
                "-1",
                "-f",
                "rawvideo",
                "-pixel_format",
                "gbrpf32le",
                "-video_size",
                f"{row.width}x{row.height}",
                "-framerate",
                "24",
            ]
        else:
            args += [
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                "-err_detect",
                "explode",
            ]
        args += ["-threads", "1", "-i", row.path.name]
    filters = [
        f"color=c=black:s={width}x{height}:r=24,format=gbrp,trim=end_frame={count},settb=1/24,setpts=N[base0]"
    ]
    video_inputs: dict[str, str] = {}
    for asset_id, index in inputs.items():
        runs = [row for row in schedule.layers if row.layer.asset_id == asset_id]
        if runs:
            labels = [f"src{index}_{number}" for number in range(len(runs))]
            filters.append(
                f"[{index}:v:0]split={len(runs)}" + "".join(f"[{label}]" for label in labels)
            )
            video_inputs.update(
                (run.layer.clip_id, label) for run, label in zip(runs, labels, strict=True)
            )
    for index, run in enumerate(schedule.layers):
        layer = run.layer
        duration = run.end - run.start
        prefix = f"l{index}"
        if layer.text is not None:
            style = layer.text
            text = texts[layer.clip_id]
            native_w, native_h = width, height
            alignment = {"left": "L", "center": "C", "right": "R"}[style.align]
            x = {"left": "0", "center": "(w-text_w)/2", "right": "w-text_w"}[style.align]
            line_spacing = round(style.size_px * (style.line_height_bp / 10000 - 1))
            draw = (
                f"drawtext=fontfile={text.font_path.name}:textfile={text.text_path.name}"
                f":expansion=none:reload=0:fontsize={style.size_px}:fontcolor={_rgba(style.fill_rgba)}"
                f":x={x}:y=(h-text_h)/2:text_align={alignment}:line_spacing={line_spacing}:fix_bounds=0"
            )
            if style.background_rgba is not None:
                draw += f":box=1:boxcolor={_rgba(style.background_rgba)}:boxborderw=0"
            if text.lines:
                # CRITICAL: align the literal line surface, not drawtext's glyph box. Its
                # non-left internal alignment clips tabs on unequal-width multiline text.
                line_height = round(style.size_px * style.line_height_bp / 10000)
                draws: list[str] = []
                for line_number, (line_path, _) in enumerate(text.lines):
                    if line_path is None:
                        continue
                    y = (
                        f"(h-{len(text.lines) * line_height})/2+"
                        f"{line_number * line_height}+( {line_height}-font_a-font_d)/2"
                    ).replace(" ", "")
                    line_draw = (
                        f"drawtext=fontfile={text.font_path.name}:textfile={line_path.name}"
                        f":expansion=none:reload=0:fontsize={style.size_px}"
                        f":fontcolor={_rgba(style.fill_rgba)}:x={x}:y={y}"
                        ":y_align=font:text_align=L:tabsize=4:fix_bounds=0"
                    )
                    if style.background_rgba is not None:
                        line_draw += f":box=1:boxcolor={_rgba(style.background_rgba)}:boxborderw=0"
                    draws.append(line_draw)
                draw = ",".join(draws)
            source = (
                f"color=c=black@0:s={width}x{height}:r=24,format=rgba,{draw},"
                f"trim=end_frame={duration},settb=1/24,setpts=N"
            )
        else:
            if layer.asset_id is None or layer.asset_id not in media:
                raise RenderGraphError()
            item = media[layer.asset_id]
            native_w, native_h = item.width, item.height
            source = f"[{video_inputs[layer.clip_id]}]"
            if item.origin == "runtime_image":
                source += f"trim=end_frame={duration},settb=1/24,setpts=N,format=rgba"
            else:
                selected: list[tuple[int, int]] = []
                for offset, source_frame in enumerate(run.source_frames):
                    if source_frame is None:
                        raise RenderGraphError()
                    if not selected or selected[-1][0] != source_frame:
                        selected.append((source_frame, offset))
                selection = "+".join(f"eq(n,{value})" for value, _ in selected)
                positions = "+".join(
                    f"eq(N,{number})*{offset}" for number, (_, offset) in enumerate(selected)
                )
                # CRITICAL: these holds come from resolved source_frame values. fps alone
                # on source timestamps would invent a second VFR selection rule.
                #
                # GUARD (measured 2026-09-10, M25-20 B-60): `tpad` stamps its clones from its own
                # frame count times one source-rate period, never from the pts `setpts` just
                # assigned. That coincides with the next output offset only when the selected
                # frames sit one source period apart -- a 24 fps source held once per frame, or
                # a 12 fps source held twice. For a source whose nominal rate is at least twice
                # the output rate (a 96 fps VFR file rounds the period to zero) the clones land
                # at or before the last selected frame, `fps` never receives a successor for it,
                # the layer ends two frames short, and the overlay below silently repeats the
                # previous source frame there. The second `setpts` therefore re-stamps every
                # clone (`N >= selected count`) to follow the last selected position by one
                # output frame each, so the tail hold never depends on the source's frame rate.
                # Do not fold it into the first `setpts` (the clones do not exist yet there) and
                # do not move `fps` ahead of `tpad` (fps drops a final frame with no successor).
                # CRITICAL: one clone supplies that successor. A time-based 150-second pad
                # creates thousands of unnecessary 1080p intermediate frames before `trim` and
                # can exhaust the renderer's 1 GiB child-memory budget on ordinary 10-second edits.
                hold = f"if(lt(N,{len(selected)}),PTS,{selected[-1][1]}+N-{len(selected)}+1)"
                source += (
                    f"select='{selection}',settb=1/24,setpts='{positions}',"
                    f"tpad=stop_mode=clone:stop=1,setpts='{hold}',"
                    f"fps=fps=24:start_time=0:round=down,"
                    f"trim=end_frame={duration},settb=1/24,setpts=N,"
                    "scale=in_color_matrix=bt709:in_range=tv:out_range=full,format=rgba"
                )
        crop, transform = layer.crop, layer.transform
        left, top = native_w * crop.left_bp // 10000, native_h * crop.top_bp // 10000
        right = (native_w * (10000 - crop.right_bp) + 9999) // 10000
        bottom = (native_h * (10000 - crop.bottom_bp) + 9999) // 10000
        crop_w, crop_h = right - left, bottom - top
        scaled_w = max(1, (crop_w * transform.scale_x_bp + 5000) // 10000)
        scaled_h = max(1, (crop_h * transform.scale_y_bp + 5000) // 10000)
        if scaled_w * scaled_h * 32 > 1024 * 1024 * 1024:
            raise RenderGraphError("resource_limit")
        source += (
            f",crop=w={crop_w}:h={crop_h}:x={left}:y={top},"
            f"scale=w={scaled_w}:h={scaled_h}:flags=bilinear,setsar=1"
        )
        angle = transform.rotation_mdeg * math.pi / 180000
        if transform.rotation_mdeg:
            source += (
                f",rotate=a={_decimal(angle)}:ow=rotw({_decimal(angle)}):"
                f"oh=roth({_decimal(angle)}):c=none"
            )
        filters.append(source + f"[{prefix}r]")
        effect = layer.effect
        if effect.kind != "none":
            # CRITICAL: pin the alpha branch on both sides; allowing downstream color
            # negotiation here makes alphaextract ambiguous and aborts actual encoding.
            filters += [
                f"[{prefix}r]split=2[{prefix}e][{prefix}a]",
                f"[{prefix}a]format=rgba,alphaextract,format=gray[{prefix}am]",
                (
                    f"[{prefix}e]scale=in_color_matrix=bt709:out_color_matrix=bt709:"
                    "in_range=full:out_range=full,format=yuv444p,"
                    f"eq=brightness={_decimal(effect.brightness_permille / 1000)}:"
                    f"contrast={_decimal(effect.contrast_permille / 1000)}:"
                    f"saturation={_decimal(effect.saturation_permille / 1000)},"
                    "scale=in_color_matrix=bt709:in_range=full:out_range=full,"
                    f"format=rgb24[{prefix}ec]"
                ),
                f"[{prefix}ec][{prefix}am]alphamerge[{prefix}fx]",
            ]
            current = prefix + "fx"
        else:
            current = prefix + "r"
        opacity = f"[{current}]colorchannelmixer=aa={_decimal(layer.opacity_bp / 10000)}"
        if run.dissolve_frames:
            opacity += f",fade=t=in:start_frame=0:nb_frames={run.dissolve_frames}:alpha=1"
        filters.append(opacity + f"[{prefix}o]")
        dx = scaled_w * (transform.anchor_x_bp / 10000 - 0.5)
        dy = scaled_h * (transform.anchor_y_bp / 10000 - 0.5)
        anchor_x = dx * math.cos(angle) - dy * math.sin(angle)
        anchor_y = dx * math.sin(angle) + dy * math.cos(angle)
        px = width / 2 + width * transform.position_x_bp / 10000 - anchor_x
        py = height / 2 + height * transform.position_y_bp / 10000 - anchor_y
        if layer.blend not in {"normal", "multiply", "screen"}:
            raise RenderGraphError()
        if layer.blend == "normal":
            # CRITICAL: pad the transformed layer before it reaches the output canvas.
            # Building a 1080p color+mask pair for every normal clip holds duplicate full-size
            # branches and can exhaust the renderer's 1 GiB child budget on a 10-second edit.
            filters += [
                (
                    f"[{prefix}o]tpad=start={run.start}:stop={count - run.end}:color=black@0,"
                    f"trim=end_frame={count},settb=1/24,setpts=N[{prefix}timeline]"
                ),
                (
                    f"[base{index}][{prefix}timeline]overlay=x={_decimal(px)}-w/2:"
                    f"y={_decimal(py)}-h/2:format=rgb:alpha=straight:shortest=1,"
                    f"format=gbrp[base{index + 1}]"
                ),
            ]
            continue
        filters += [
            f"color=c=black@0:s={width}x{height}:r=24,format=rgba,trim=end_frame={duration},settb=1/24,setpts=N[{prefix}blank]",
            (
                f"[{prefix}blank][{prefix}o]overlay=x={_decimal(px)}-w/2:"
                f"y={_decimal(py)}-h/2:format=rgb:alpha=straight:shortest=1,format=rgba,"
                f"tpad=start={run.start}:stop={count - run.end}:color=black@0,"
                f"trim=end_frame={count},settb=1/24,setpts=N,"
                f"split=2[{prefix}rgb][{prefix}alpha]"
            ),
            f"[{prefix}rgb]format=gbrp[{prefix}color]",
            f"[{prefix}alpha]alphaextract,format=gbrp[{prefix}mask]",
        ]
        target = prefix + "color"
        base = f"base{index}"
        filters += [
            f"[{base}]split=2[{prefix}base][{prefix}mixbase]",
            f"[{target}][{prefix}mixbase]blend=all_mode={layer.blend}[{prefix}mixed]",
        ]
        base, target = prefix + "base", prefix + "mixed"
        filters.append(f"[{base}][{target}][{prefix}mask]maskedmerge[base{index + 1}]")
    audio_labels: list[str] = []
    clip_audio: dict[str | None, PlanClipAudio] = {
        entry.clip_id: entry for entry in plan.clip_audio
    }
    if len(clip_audio) != len(plan.clip_audio):
        raise RenderGraphError()
    if schedule.emits_audio_stream:
        for index, run_audio in enumerate(schedule.audio):
            samples = run_audio.end - run_audio.start
            if run_audio.asset_id is None:
                audio_filter = (
                    f"anullsrc=r=48000:cl=mono,atrim=end_sample={samples},asetpts=N/SR/TB"
                )
            else:
                input_index = inputs[run_audio.asset_id]
                audio_filter = (
                    f"[{input_index}:a:0]aresample=48000,aformat=channel_layouts=mono,"
                    f"atrim=start_sample={run_audio.source_start}:"
                    f"end_sample={run_audio.source_start + samples},asetpts=N/SR/TB"
                )
                # A run of a clip without adjustments keeps the chain it had before they
                # existed, byte for byte.
                entry = clip_audio.get(run_audio.clip_id)
                if entry is not None:
                    audio_filter += _clip_audio_filters(entry, run_audio)
            audio_labels.append(f"audio{index}")
            filters.append(audio_filter + f"[audio{index}]")
        filters.append(
            "".join(f"[{label}]" for label in audio_labels)
            + f"concat=n={len(audio_labels)}:v=0:a=1,"
            f"atrim=end_sample={count * 2000},asetpts=N/SR/TB[final_a]"
        )
    filters.append(
        f"[base{len(schedule.layers)}]scale=in_range=full:out_range=tv:out_color_matrix=bt709:flags=accurate_rnd+bitexact,format=yuv420p,setsar=1,settb=1/24,setpts=N[final_v]"
    )
    args += ["-map", "[final_v]"]
    args += (
        ["-map", "[final_a]", "-c:a", "aac", "-ar", "48000", "-ac", "1"]
        if schedule.emits_audio_stream
        else ["-an"]
    )
    args += [
        "-frames:v",
        str(count),
        "-c:v",
        "libx264",
        "-threads:v",
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
        "24",
        "-movie_timescale",
        "48000",
        "-map_metadata",
        "-1",
        "-f",
        "mp4",
        "output.mp4",
    ]
    graph = ";\n".join(filters)
    if len(graph.encode("utf-8")) > 16 * 1024 * 1024:
        raise RenderGraphError("resource_limit")
    return RenderGraphProgram(tuple(args), graph)
