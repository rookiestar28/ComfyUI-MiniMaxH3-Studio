"""M25-20 F1 corrective, browser half: the real synthetic source media the browser presents.

The corpus's declared picture (`comfyui_h3_context.core.semantic_conformance_media`) is only
readable back out of a canvas if the browser actually decodes media painted to that exact
specification -- the frame-identity row, the labelled colour patches, the embedded audio bursts.
The render stage already builds this media for its own ffmpeg pipeline
(`scripts/nle_semantic_render.py`'s `_paint_frame`/`_build_source_video`); this script *imports*
those same functions (read-only reuse, nothing in that module is edited) and calls them once to
produce three static files the Playwright harness can serve, exactly the way `openIntegratedShell`
already serves a static `cfr-primary.mp4` for the ordinary NLE journeys. There is no independent
re-implementation of the paint routine here: every pixel comes from the one function every stage of
the corpus already agrees on.

The image reference source (`img-overlay`) has no video timeline, so it is encoded as a single PNG
frame with the same `_paint_frame` bytes, through the same pinned ffmpeg (rawvideo -> png), rather
than a second raster path.

Usage:
    python scripts/nle_semantic_browser_media.py --output-dir <dir> \
        --ffmpeg <path-to-ffmpeg> [--asset <asset-id> ...]

Writes one `<asset-id>.mp4` per video source and one `<asset-id>.png` per image source every corpus
base declares into `<dir>`, or only the sources `--asset` names, and `media_constants.json`.
"""

from __future__ import annotations

import argparse
import subprocess  # noqa: S404 -- fixed argv, caller-supplied executable, no shell
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402
import json  # noqa: E402

from comfyui_h3_context.core.semantic_conformance_media import (  # noqa: E402
    AUDIO_BURST_SAMPLES,
    AUDIO_BURST_STARTS,
    AUDIO_SAMPLE_RATE,
    FRAME_ID_BITS,
    FRAME_ID_CELL_PITCH_PX,
    FRAME_ID_MARK_PX,
    FRAME_ID_ONE_RGB,
    FRAME_ID_ORIGIN_PX,
    FRAME_ID_ZERO_RGB,
    GEOMETRY_LUMA_THRESHOLD,
    PATCH_SIZE_PX,
    SOURCE_HEIGHT,
    SOURCE_PROFILES,
    SOURCE_WIDTH,
    profile_for,
)
from scripts.nle_semantic_render import (  # noqa: E402
    BASE_IMAGE_ASSET,
    BASE_VIDEO_ASSETS,
    DECODE_TIMEOUT_SECONDS,
    SOURCE_FPS,
    RenderStageError,
    _build_source_video,
    _paint_frame,
)


def _build_image_png(ffmpeg: Path, destination: Path, asset_id: str = "img-overlay") -> None:
    """Encode one image asset's single frame as a PNG with the exact `_paint_frame` bytes.

    GUARD: the raster size comes from the profile, never from the module's `SOURCE_WIDTH`. The
    high-resolution assets are 512-square while the accepted ones are 128-square, and a raw frame
    decoded at the wrong size is not an error -- it is a picture of the right bytes in the wrong
    shape, which reads back as landmarks nobody declared.
    """

    profile = profile_for(asset_id)
    if profile is None:
        raise RenderStageError("image_profile_missing")
    raw = destination.with_suffix(".raw")
    raw.write_bytes(bytes(_paint_frame(profile, 0)))
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
        "-i",
        str(raw),
        "-frames:v",
        "1",
        str(destination),
    ]
    completed = subprocess.run(  # noqa: S603 -- fixed argv, caller-supplied executable, no shell
        args, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS, check=False
    )
    raw.unlink(missing_ok=True)
    if completed.returncode != 0 or not destination.is_file():
        raise RenderStageError("image_fixture_encode_failed")


def build_all(
    output_dir: Path, ffmpeg: Path, only: frozenset[str] | None = None
) -> dict[str, Path]:
    """Every source every corpus base declares, so the browser decodes the same files.

    `only` names the sources to build and leaves every other file in `output_dir` as it is. A base
    that adds a source builds that source alone: re-encoding the committed files would replace
    bytes the accepted journeys were observed against for no change of their declaration.
    """

    video_assets = sorted({asset for assets in BASE_VIDEO_ASSETS.values() for asset in assets})
    image_assets = sorted(set(BASE_IMAGE_ASSET.values()))
    if only is not None:
        undeclared = only - set(video_assets) - set(image_assets)
        if undeclared:
            raise RenderStageError("asset_undeclared")
        video_assets = [asset for asset in video_assets if asset in only]
        image_assets = [asset for asset in image_assets if asset in only]
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for asset_id in video_assets:
        video_profile = profile_for(asset_id)
        if video_profile is None:
            raise RenderStageError("video_profile_missing")
        paths[asset_id] = output_dir / f"{asset_id}.mp4"
        _build_source_video(ffmpeg, paths[asset_id], profile=video_profile)
    for asset_id in image_assets:
        paths[asset_id] = output_dir / f"{asset_id}.png"
        _build_image_png(ffmpeg, paths[asset_id], asset_id)
    return paths


def constants_document() -> dict[str, object]:
    """Every number and label the browser-side extractor needs, read from the one module that
    declares them (`semantic_conformance_media`), never retyped by hand in the TypeScript helper.
    """

    return {
        "schema": "h3.context.nle_semantic_media_constants.v1",
        "source_width": SOURCE_WIDTH,
        "source_height": SOURCE_HEIGHT,
        "geometry_luma_threshold": GEOMETRY_LUMA_THRESHOLD,
        "patch_size_px": PATCH_SIZE_PX,
        "frame_id_bits": FRAME_ID_BITS,
        "frame_id_origin_px": list(FRAME_ID_ORIGIN_PX),
        "frame_id_cell_pitch_px": FRAME_ID_CELL_PITCH_PX,
        "frame_id_mark_px": FRAME_ID_MARK_PX,
        "frame_id_one_rgb": list(FRAME_ID_ONE_RGB),
        "frame_id_zero_rgb": list(FRAME_ID_ZERO_RGB),
        "audio_sample_rate": AUDIO_SAMPLE_RATE,
        "audio_burst_samples": AUDIO_BURST_SAMPLES,
        "audio_burst_starts": list(AUDIO_BURST_STARTS),
        "source_profiles": [
            {
                "asset_id": profile.asset_id,
                "field": list(profile.field),
                "field_sample": list(profile.field_sample),
                "carries_frame_id": profile.carries_frame_id,
                "carries_audio": profile.carries_audio,
                "frame_count": profile.frame_count,
                "patches": [dataclasses.asdict(patch) for patch in profile.patches],
            }
            for profile in SOURCE_PROFILES
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument(
        "--skip-media",
        action="store_true",
        help="only (re)write media_constants.json, skipping the slow ffmpeg encode",
    )
    parser.add_argument(
        "--asset",
        action="append",
        default=None,
        help="build only this source (repeatable); every other media file is left as it is",
    )
    args = parser.parse_args(argv)

    if not args.skip_media:
        if not args.ffmpeg.is_file():
            print(f"ffmpeg not found: {args.ffmpeg}", file=sys.stderr)
            return 2
        only = None if args.asset is None else frozenset(args.asset)
        paths = build_all(args.output_dir, args.ffmpeg, only)
        for name, path in paths.items():
            print(f"{name}: {path} ({path.stat().st_size} bytes)")
        _ = SOURCE_FPS  # imported for parity with the render stage; not used directly here
    args.output_dir.mkdir(parents=True, exist_ok=True)
    constants_path = args.output_dir / "media_constants.json"
    constants_path.write_text(
        json.dumps(constants_document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"constants: {constants_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
