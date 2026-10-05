"""Private source preparation; no renderer qualification or public execution admission."""

from __future__ import annotations

import ctypes
import hashlib
import math
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, SupportsIndex

from ..core.authoring_render_jobs import render_plan_fingerprint
from ..core.composition_contract import TextStyle
from ..core.render_planner import RenderPlanV1
from ..core.safe_paths import UnsafePathError, validate_regular_file
from .authoring_fonts import AuthoringFontError, PackagedFontManifest, require_text_coverage
from .authoring_image_source import IMAGE_SOURCE_PROFILE
from .authoring_render_leases import AuthoringRenderJobSources
from .authoring_render_process import (
    PinnedRenderExecutable,
    ProcessControl,
    RenderProcessError,
    WindowsRenderProcessSession,
    _guard,
    _kernel,
)
from .authoring_render_store import RenderStage
from .segment_artifact_store import _msvcrt_open_osfhandle, _validated_directories


class RenderPreparationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in {
            "source_invalid",
            "plan_mismatch",
            "runtime_unavailable",
            "font_unavailable",
        }:
            code = "source_invalid"
        self.code = code
        super().__init__(code)


@contextmanager
def _pin_stage_inputs(
    stage: RenderStage, fingerprints: dict[Path, str], control: ProcessControl
) -> Iterator[None]:
    """Hold exact privately staged bytes through the child lifetime, without reopening races."""
    _guard(control)
    if sys.platform != "win32":
        raise RenderPreparationError("runtime_unavailable")
    if type(stage) is not RenderStage or len(fingerprints) > 4095:
        raise RenderPreparationError("source_invalid")
    stage.check_budget()
    with ExitStack() as scope:
        try:
            directory = stage.output_path.parent
            scope.enter_context(_validated_directories(directory))
            for path, expected_digest in fingerprints.items():
                _guard(control)
                if (
                    path.parent != directory
                    or path.name not in stage._files
                    or path.name == "output.mp4"
                ):
                    raise RenderPreparationError("source_invalid")
                admitted = validate_regular_file(path, maximum_bytes=64 * 1024 * 1024)
                # CRITICAL: read-only sharing denies writes and replacement until encoding
                # ends; a size/mtime recheck cannot detect same-stat content substitution.
                kernel = _kernel()
                handle = kernel.CreateFileW(
                    str(admitted), 0x80000000, 0x1, None, 3, 0x00200080, None
                )
                if not handle or handle == ctypes.c_void_p(-1).value:
                    raise RenderPreparationError("source_invalid")
                try:
                    descriptor = _msvcrt_open_osfhandle(
                        int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
                    )
                except BaseException:
                    kernel.CloseHandle(handle)
                    raise
                scope.callback(os.close, descriptor)
                metadata = os.fstat(descriptor)
                expected = admitted.lstat()
                if metadata.st_nlink != 1 or (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                ) != (expected.st_dev, expected.st_ino, expected.st_size):
                    raise RenderPreparationError("source_invalid")
                digest = hashlib.sha256()
                total = 0
                while True:
                    _guard(control)
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > 64 * 1024 * 1024:
                        raise RenderPreparationError("source_invalid")
                    digest.update(block)
                if total != metadata.st_size or "sha256:" + digest.hexdigest() != expected_digest:
                    raise RenderPreparationError("source_invalid")
            stage.check_budget()
        except (OSError, UnsafePathError):
            raise RenderPreparationError("source_invalid") from None
        yield


def pack_planar_image(pixels: bytes, *, width: int, height: int, control: ProcessControl) -> bytes:
    """Reorder little-endian float32 RGB into GBR planes without changing code values."""
    _guard(control)
    if (
        type(pixels) is not bytes
        or type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 8192
        or not 1 <= height <= 8192
        or width * height > 4_194_304
        or len(pixels) != width * height * 12
        or len(pixels) > 64 * 1024 * 1024
    ):
        raise RenderPreparationError("source_invalid")
    if sys.byteorder != "little":
        raise RenderPreparationError("runtime_unavailable")
    with memoryview(pixels).cast("f") as values:
        for index, value in enumerate(values):
            if index % 16_384 == 0:
                _guard(control)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise RenderPreparationError("source_invalid")
        # CRITICAL: preserve source float bits, including signed zero; quantizing or
        # clamping here changes the source before the qualified color transform runs.
        planes: list[bytes] = []
        for channel in (1, 2, 0):
            _guard(control)
            with values[channel::3] as plane:
                planes.append(plane.tobytes())
        result = b"".join(planes)
    _guard(control)
    return result


@dataclass(frozen=True, slots=True, repr=False)
class PreparedRenderMedia:
    asset_id: str
    origin: str
    path: Path
    width: int
    height: int
    source_fingerprint: str
    staged_fingerprint: str

    def __repr__(self) -> str:
        return "<PreparedRenderMedia opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("prepared media are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("prepared media are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prepared media are not serializable")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedRenderText:
    clip_id: str
    text_path: Path
    font_path: Path
    text_fingerprint: str
    font_fingerprint: str
    lines: tuple[tuple[Path | None, str], ...] = ()

    def __repr__(self) -> str:
        return "<PreparedRenderText opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("prepared text is not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("prepared text is not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prepared text is not serializable")


def prepare_render_text(
    *,
    clip_id: str,
    style: TextStyle,
    fonts: PackagedFontManifest,
    stage: RenderStage,
    control: ProcessControl,
) -> PreparedRenderText:
    """Keep literal text outside filter syntax and pin the exact packaged font bytes."""
    _guard(control)
    if (
        type(style) is not TextStyle
        or type(fonts) is not PackagedFontManifest
        or type(stage) is not RenderStage
        or type(style.content) is not str
        or not 1 <= len(style.content) <= 2048
    ):
        raise RenderPreparationError("plan_mismatch")
    try:
        face = require_text_coverage(
            fonts, style.font_asset_id, style.weight, style.style, style.content
        )
        font_payload = face.read_verified_bytes()
    except AuthoringFontError:
        # SECURITY: a missing glyph is a closed failure, not permission to search system
        # fonts or substitute a different face and silently alter the accepted title.
        raise RenderPreparationError("font_unavailable") from None
    _guard(control)
    text_payload = style.content.encode("utf-8")
    font_path = stage.write_input(font_payload, suffix=".bin")
    text_path = stage.write_input(text_payload, suffix=".txt")
    lines: list[tuple[Path | None, str]] = []
    if "\t" in style.content and style.align != "left":
        # CRITICAL: native centered/right multiline tabs clip glyphs. Keep literal line bytes
        # separately for left-internal rasterization; never expand tabs or rewrite the snapshot.
        for line in style.content.split("\n"):
            _guard(control)
            payload = line.encode("utf-8")
            path = stage.write_input(payload, suffix=".txt") if payload else None
            lines.append((path, "sha256:" + hashlib.sha256(payload).hexdigest()))
    _guard(control)
    stage.check_budget()
    return PreparedRenderText(
        clip_id,
        text_path,
        font_path,
        "sha256:" + hashlib.sha256(text_payload).hexdigest(),
        face.file_fingerprint,
        tuple(lines),
    )


@dataclass(frozen=True, slots=True, repr=False)
class PreparedRenderAssets:
    media: tuple[PreparedRenderMedia, ...]
    texts: tuple[PreparedRenderText, ...]
    plan_fingerprint: str
    _stage: RenderStage

    def __repr__(self) -> str:
        return "<PreparedRenderAssets opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("prepared assets are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("prepared assets are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prepared assets are not serializable")


def prepare_render_assets(
    *,
    plan: RenderPlanV1,
    sources: AuthoringRenderJobSources,
    stage: RenderStage,
    control: ProcessControl,
) -> PreparedRenderAssets:
    """Stage exact active leased media. Caller owns cleanup on every failed attempt."""
    _guard(control)
    if (
        type(plan) is not RenderPlanV1
        or type(sources) is not AuthoringRenderJobSources
        or type(stage) is not RenderStage
    ):
        raise RenderPreparationError("plan_mismatch")
    if sources.confirm_currentness() != plan.source_currentness_claim:
        raise RenderPreparationError("plan_mismatch")
    fonts = sources._fonts
    if (
        fonts.manifest_fingerprint != plan.font_manifest_fingerprint
        or fonts.package_fingerprint != plan.font_package_fingerprint
    ):
        raise RenderPreparationError("plan_mismatch")
    stage.check_budget()
    active: set[str] = set()
    titles: dict[str, TextStyle] = {}
    for chunk in plan.resolved_operations:
        _guard(control)
        for frame in chunk.frames:
            for layer in frame.layers:
                if layer.text is not None:
                    if layer.clip_id in titles and titles[layer.clip_id] != layer.text:
                        raise RenderPreparationError("plan_mismatch")
                    titles[layer.clip_id] = layer.text
                elif layer.asset_id is not None:
                    active.add(layer.asset_id)
            if frame.audio_span is not None:
                active.add(frame.audio_span.asset_id)
    facts = {fact.asset_id: fact for fact in plan.source_bindings}
    if len(facts) != len(plan.source_bindings) or not active.issubset(facts):
        raise RenderPreparationError("plan_mismatch")
    if len(titles) > plan.limits.max_title_bindings:
        raise RenderPreparationError("plan_mismatch")
    media: list[PreparedRenderMedia] = []
    for asset_id in sorted(active):
        _guard(control)
        fact = facts[asset_id]
        if (
            fact.disposition != "enabled"
            # CRITICAL: generated artifacts use the same verified MP4 path as context video;
            # rejecting that origin strands an admitted Production import before encoding.
            or fact.origin not in {"runtime_image", "context_video", "generated"}
            or type(fact.width) is not int
            or type(fact.height) is not int
            or fact.source_fingerprint is None
        ):
            raise RenderPreparationError("plan_mismatch")
        payload = sources.read_source(asset_id)
        if len(payload) != fact.size_bytes or len(payload) > 64 * 1024 * 1024:
            raise RenderPreparationError("plan_mismatch")
        digest = hashlib.sha256()
        if fact.origin == "runtime_image":
            digest.update(f"{IMAGE_SOURCE_PROFILE}:{fact.width}:{fact.height}:".encode("ascii"))
        digest.update(payload)
        if "sha256:" + digest.hexdigest() != fact.source_fingerprint:
            raise RenderPreparationError("plan_mismatch")
        if fact.origin == "runtime_image":
            payload = pack_planar_image(
                payload, width=fact.width, height=fact.height, control=control
            )
        _guard(control)
        path = stage.write_input(
            payload, suffix=".bin" if fact.origin == "runtime_image" else ".mp4"
        )
        media.append(
            PreparedRenderMedia(
                asset_id=asset_id,
                origin=fact.origin,
                path=path,
                width=fact.width,
                height=fact.height,
                source_fingerprint=fact.source_fingerprint,
                staged_fingerprint="sha256:" + hashlib.sha256(payload).hexdigest(),
            )
        )
    texts = tuple(
        prepare_render_text(clip_id=clip_id, style=style, fonts=fonts, stage=stage, control=control)
        for clip_id, style in sorted(titles.items())
    )
    _guard(control)
    if sources.confirm_currentness() != plan.source_currentness_claim:
        raise RenderPreparationError("plan_mismatch")
    stage.check_budget()
    return PreparedRenderAssets(tuple(media), texts, render_plan_fingerprint(plan), stage)


def run_prepared_render(
    *,
    plan: RenderPlanV1,
    prepared: PreparedRenderAssets,
    renderer: PinnedRenderExecutable,
    session: WindowsRenderProcessSession,
    control: ProcessControl,
) -> None:
    """Execute only the closed graph under the caller's cumulative job; never publish."""
    from .authoring_render_graph import build_render_graph

    _guard(control)
    if (
        type(prepared) is not PreparedRenderAssets
        or type(renderer) is not PinnedRenderExecutable
        or type(session) is not WindowsRenderProcessSession
    ):
        raise RenderPreparationError("plan_mismatch")
    program = build_render_graph(plan=plan, prepared=prepared)
    stage = prepared._stage
    if stage.output_path.exists():
        raise RenderProcessError("process_failed")
    graph_bytes = program.filter_graph.encode("utf-8")
    graph_path = stage.write_input(graph_bytes, suffix=".ffgraph")
    fingerprints = {row.path: row.staged_fingerprint for row in prepared.media}
    for text in prepared.texts:
        fingerprints[text.text_path] = text.text_fingerprint
        fingerprints[text.font_path] = text.font_fingerprint
        for path, fingerprint in text.lines:
            if path is not None:
                fingerprints[path] = fingerprint
    fingerprints[graph_path] = "sha256:" + hashlib.sha256(graph_bytes).hexdigest()
    with _pin_stage_inputs(stage, fingerprints, control):
        measured = session.run(
            renderer,
            ("-filter_complex_script", graph_path.name, *program.arguments),
            cwd=stage.output_path.parent,
            control=control,
            check_staging=stage.check_budget,
        )
        if (
            measured.exit_code
            or measured.diagnostic_bytes
            or measured.stdout
            or measured.active_processes
            or not measured.limits_verified
        ):
            raise RenderProcessError("process_failed")
        _guard(control)
        stage.check_budget()
        try:
            output = validate_regular_file(
                stage.output_path, maximum_bytes=stage._store._limits.max_output_bytes
            )
            if output.stat().st_size == 0 or output.stat().st_nlink != 1:
                raise RenderProcessError("process_failed")
        except (OSError, UnsafePathError):
            raise RenderProcessError("process_failed") from None
