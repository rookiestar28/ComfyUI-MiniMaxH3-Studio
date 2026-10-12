"""H3 temporal lattice specification expressed independently of native source code.

Authority: Comfy-Org/ComfyUI, nodes_minimax_h3.py at the public revision below.
The admissible frame sequence starts at five, with seventeen frames per subsequent group;
each video group adds five latent positions after the initial two. Audio spans forty latent
positions per second at twenty-four video frames per second, using nearest-even rounding.
"""

from __future__ import annotations

SOURCE_ANCHORS = (
    "native.frames.alignment@b323a345",
    "native.latents.geometry@b323a345",
    "template.i2v@5097de61",
)

OFFICIAL_TEMPLATE_REVISION = "5097de61ef09fe75466716ac0b200515f5ea078f"  # pragma: allowlist secret
NATIVE_NODE_REVISION = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
NATIVE_NODE_SHA256 = "sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064"
NATIVE_NODE_BYTES = 15728
NATIVE_NODE_RETRIEVED = "2026-08-19"
NATIVE_FPS = 24
NATIVE_AUDIO_LATENT_FPS = 40


def native_align_frame_count(count: int) -> int:
    complete, remainder = divmod(count - 5, 17)
    groups = complete + bool(remainder)
    return 5 + 17 * groups


def official_expression(seconds: float) -> int:
    nominal = max(round(seconds * NATIVE_FPS), 5)
    return native_align_frame_count(nominal)


def native_video_latent_t(frame_count: int) -> int:
    complete_groups = max((frame_count - 5) // 17, 0)
    return 2 + 5 * complete_groups


def native_temporal_shape(nominal_length: int) -> tuple[int, int, int]:
    frames = native_align_frame_count(max(nominal_length, 5))
    audio_duration = frames / NATIVE_FPS
    return frames, native_video_latent_t(frames), round(audio_duration * NATIVE_AUDIO_LATENT_FPS)
