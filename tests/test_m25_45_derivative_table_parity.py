"""M25-45: the derivative byte and edge tables are one contract across the lease route.

Three copies of the same numbers exist by design -- the backend that generates and admits a body,
the codec that decodes the wire in the browser, and the host lease that reads the body -- because
each is a trust boundary that must refuse on its own. A copy that drifts does not fail loudly: the
generator produces a body the browser then refuses, or worse, the browser accepts a body the
backend would not have published. So every copy is read from its own source here and compared.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from comfyui_h3_context.adapters.authoring_derivative_generator import (
    VIDEO_PROXY_GOP,
    VIDEO_PROXY_MAX_BITS_PER_SECOND,
    VIDEO_PROXY_MAX_BYTES,
    VIDEO_PROXY_MAX_EDGE,
)
from comfyui_h3_context.core.authoring_media import (
    MAX_CACHE_BYTES,
    DerivativeKind,
    derivative_byte_limit,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
CODEC: Final = REPOSITORY_ROOT / "frontend" / "src" / "contracts" / "authoringMediaLeaseCodec.ts"
HOST_LEASE: Final = REPOSITORY_ROOT / "frontend" / "src" / "host" / "authoringMediaSourceLease.ts"
RESOURCES: Final = (
    REPOSITORY_ROOT / "frontend" / "src" / "runtime" / "visualCompositionResources.ts"
)

KINDS: Final[tuple[DerivativeKind, ...]] = (
    "video_proxy",
    "audio_preview",
    "frame_timing_index",
    "thumbnail",
    "filmstrip",
    "audio_peaks",
    "image_proxy",
    "packaged_font_face",
)


def _typescript_table(source: Path, opening: str) -> dict[str, int]:
    text = source.read_text(encoding="utf-8")
    start = text.index(opening) + len(opening)
    body = text[start : text.index("}", start)]
    table: dict[str, int] = {}
    for name, expression in re.findall(r"(\w+):\s*([0-9_ */]+),", body):
        table[name] = int(eval(expression.replace("_", "")))  # noqa: S307 - literal arithmetic
    return table


def test_every_kind_has_the_same_byte_ceiling_on_both_ends() -> None:
    backend = {kind: derivative_byte_limit(kind) for kind in KINDS}
    codec = _typescript_table(
        CODEC,
        "const byteLimits: Readonly<Record<AuthoringMediaDerivativeKind, number>> = {",
    )
    host = _typescript_table(HOST_LEASE, "const BYTE_LIMITS = Object.freeze({")
    assert codec == backend
    assert host == backend


def test_the_video_proxy_ceilings_are_the_migrated_ones() -> None:
    assert derivative_byte_limit("video_proxy") == VIDEO_PROXY_MAX_BYTES
    assert VIDEO_PROXY_MAX_BYTES == 24 * 1024 * 1024
    assert VIDEO_PROXY_MAX_EDGE == 1280
    assert VIDEO_PROXY_MAX_BITS_PER_SECOND == 6_000_000
    assert VIDEO_PROXY_GOP == 12


def test_the_cache_holds_three_proxies_two_indexes_and_headroom() -> None:
    # The aggregate the plan fixes: three video proxies, two 16 MiB image proxies and 8 MiB of
    # index, thumbnail and font headroom must all fit at once, or a legitimate three-owner
    # composition evicts itself while it is still being presented.
    required = (
        3 * VIDEO_PROXY_MAX_BYTES + 2 * derivative_byte_limit("image_proxy") + 8 * 1024 * 1024
    )
    assert MAX_CACHE_BYTES >= required
    assert MAX_CACHE_BYTES == 128 * 1024 * 1024
    assert 2 * derivative_byte_limit("audio_preview") == 16 * 1024 * 1024


def test_the_geometry_edge_refusal_is_the_same_number_everywhere() -> None:
    codec = CODEC.read_text(encoding="utf-8")
    assert codec.count('kind === "video_proxy" ? 1_280 : 16_384,') == 2
    assert "? 320 :" not in codec
    resources = RESOURCES.read_text(encoding="utf-8")
    assert "geometry.derivativeWidth > 1_280" in resources
    assert "geometry.derivativeHeight > 1_280" in resources
    media = (REPOSITORY_ROOT / "comfyui_h3_context" / "core" / "authoring_media.py").read_text(
        encoding="utf-8"
    )
    assert "max(self.geometry.derivative_width, self.geometry.derivative_height) > 1280" in media


def test_the_derivative_profile_identifier_moved_with_the_tables() -> None:
    from comfyui_h3_context.core.authoring_media import DERIVATIVE_PROFILE_ID

    assert DERIVATIVE_PROFILE_ID == "h3.authoring.media_derivatives.v6"
    codec = CODEC.read_text(encoding="utf-8")
    assert f'"{DERIVATIVE_PROFILE_ID}" as const' in codec
