"""M25-45 AC45-06 case (a), backend layer: a 1280 px proxy above the old 8 MiB ceiling.

The proxy migration raised the video body ceiling because a 1280 px proxy of an ordinary source
does not fit in 8 MiB. That is a claim about the real encoder's real output, not about a number in
a table, so this case produces one with the pinned pair and carries it through the boundary that
admits a generated body: `admit_media_derivative` and the claim's own `generate`, which reads the
encoder's file under the ceiling and publishes it.

The browser half of the case -- the same size of body arriving through the real lease route and
decoded by the real browser codec -- is `frontend/tests/e2e/journeys/mediaSourceLeases.spec.ts`.
Each end admits a body on its own, so each end is proved on its own.

Runs only where the authorized FFmpeg/FFprobe pair is supplied explicitly; there is no fixture
substitute, because a recorded body would prove the ceiling arithmetic and nothing about the codec.
"""

from __future__ import annotations

import hashlib
import subprocess  # noqa: S404 -- fixed argv, caller-supplied executable, no shell
import time
from pathlib import Path
from typing import Final

from test_m25_authoring_audio_rate_compatibility import _qualified_tools

from comfyui_h3_context.adapters.authoring_derivative_generator import (
    VIDEO_PROXY_MAX_BYTES,
    VIDEO_PROXY_MAX_EDGE,
    AuthoringDerivativeGenerator,
)
from comfyui_h3_context.adapters.authoring_video_facts import probe_authoring_video_facts

#: The ceiling this item raised. A proxy body at or below it would prove nothing about the change.
OLD_VIDEO_PROXY_MAX_BYTES: Final = 8 * 1024 * 1024
#: The source: long enough that the generator's own per-second target exceeds the old ceiling (the
#: target is capped at 6 Mbit/s, so 8 MiB needs more than eleven seconds), and noisy enough that
#: the encoder actually spends that budget instead of compressing a synthetic pattern away.
SOURCE_SECONDS: Final = 16
SOURCE_RATE: Final = 24
SOURCE_WIDTH, SOURCE_HEIGHT = 1280, 720


def _encode(ffmpeg: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 -- the caller-supplied authorized executable, fixed argv
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=300,
    )


def _source(ffmpeg: Path, target: Path) -> None:
    """A real 1280 x 720 H.264 source whose content cannot be compressed into the old ceiling."""

    _encode(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={SOURCE_WIDTH}x{SOURCE_HEIGHT}:rate={SOURCE_RATE}"
        f":duration={SOURCE_SECONDS}",
        "-vf",
        "noise=alls=64:allf=t+u,"
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,setsar=0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "12M",
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
        "-movflags",
        "+faststart",
        "-y",
        str(target),
    )


def test_a_real_1280_px_proxy_above_the_old_ceiling_is_generated_and_admitted(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe, adapter = _qualified_tools(tmp_path)
    encoded = (tmp_path / "highres-source.mp4").resolve()
    _source(ffmpeg, encoded)
    original = encoded.read_bytes()
    assert len(original) < 64 * 1024 * 1024, "the source itself must stay inside the staging bound"

    facts = probe_authoring_video_facts(encoded, adapter, time.monotonic() + 120)
    assert (facts.width, facts.height) == (SOURCE_WIDTH, SOURCE_HEIGHT)
    assert facts.frame_count == SOURCE_SECONDS * SOURCE_RATE

    scratch = (tmp_path / "proxy-scratch").resolve()
    generator = AuthoringDerivativeGenerator(
        ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, scratch_root=scratch
    )
    proxy = generator.generate_video_proxy(
        encoded,
        facts,
        expected_source_fingerprint="sha256:" + hashlib.sha256(original).hexdigest(),
        include_embedded_audio=False,
        deadline=time.monotonic() + 300,
    )

    # The point of the case: a real proxy of a real 1280 px source does not fit in the old ceiling,
    # and does fit in the new one. Were this body under 8 MiB the migration would be unproved --
    # so the assertion is on the body the encoder produced, never on the limit constant.
    assert len(proxy.body) > OLD_VIDEO_PROXY_MAX_BYTES, len(proxy.body)
    assert len(proxy.body) <= VIDEO_PROXY_MAX_BYTES
    assert proxy.byte_count == len(proxy.body)
    assert proxy.media_type == "video/mp4"
    assert max(proxy.width, proxy.height) == VIDEO_PROXY_MAX_EDGE
    assert (proxy.width, proxy.height) == (SOURCE_WIDTH, SOURCE_HEIGHT)
    assert proxy.content_fingerprint == "sha256:" + hashlib.sha256(bytes(proxy.body)).hexdigest()
    # The original is never the derivative, and nothing is left behind in the scratch tree.
    assert encoded.read_bytes() == original
    assert not list(scratch.iterdir()) if scratch.is_dir() else True
