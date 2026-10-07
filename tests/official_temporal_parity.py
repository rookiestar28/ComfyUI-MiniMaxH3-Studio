"""The one fixture that transcribes H3's official temporal arithmetic.

Two accepted items assert parity against upstream: `M17-25` owns the duration side, adopted from the
official workflow templates, and `M20-01` owns the frame and latent side, read from the native node.
They are different upstream artifacts, but they express one lattice, and a second transcription of
it would be the same duplication defect the single-authority rule exists to remove -- only in test
form, where it is harder to see. So both parity rows read this module and neither holds a copy.

Everything here is transcribed **independently of the implementation under test**. That is the whole
value: a fixture derived from the code it checks proves only that the code equals itself.

The arithmetic is written exactly as upstream publishes it, literals included. This is the one place
in the repository where a bare `17` is correct, because the point is to fail if
`comfyui_h3_context.core.length` ever stops agreeing with the number upstream actually uses.
"""

from __future__ import annotations

# --- Duration side: the official workflow templates (M17-25) -----------------------------------
#
#   package   Comfy-Org/workflow_templates (MIT)
#   revision  5097de61ef09fe75466716ac0b200515f5ea078f, dated 2026-08-06
#   files     templates/video_minimax_h3_t2v.json and its i2v and r2v siblings
#   node      ComfyMathExpression, whose evaluation environment binds "round" to the Python
#             built-in (comfy_extras/nodes_math.py)
#   retrieved 2026-08-18
#
# The upstream commit these parity claims are pinned to. It is public repository metadata, not a
# credential; the pragma is the repo convention for the same shape in `.pre-commit-config.yaml`.
OFFICIAL_TEMPLATE_REVISION = "5097de61ef09fe75466716ac0b200515f5ea078f"  # pragma: allowlist secret

# --- Frame and latent side: the native node (M20-01) -------------------------------------------
#
#   file      comfy_extras/nodes_minimax_h3.py
#   host      ComfyUI 0.32.0
#   revision  b323a345bbbfb2f3a95b5b73b68eb7919a26515e
#   retrieved 2026-08-19
#
# The identity is the one `M19-07` recorded and then reproduced after its distinct review, which is
# the evidence that the subject did not move underneath the correction. It is transcribed here
# rather than read from disk on purpose: the pinned host is not present in CI, and a parity row that
# silently skips where the source is absent is not a parity row.
NATIVE_NODE_REVISION = "b323a345bbbfb2f3a95b5b73b68eb7919a26515e"  # pragma: allowlist secret
NATIVE_NODE_SHA256 = "sha256:f767df4074b908efb345f5a87c2fd263ba82c12e65bcca932846207cc213e064"
NATIVE_NODE_BYTES = 15728
NATIVE_NODE_RETRIEVED = "2026-08-19"

#: The node's declared module constants, transcribed.
NATIVE_FPS = 24
NATIVE_AUDIO_LATENT_FPS = 40


def official_expression(seconds: float) -> int:
    """Evaluate `max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17`."""

    a = seconds
    return max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17


def native_align_frame_count(n: int) -> int:
    """Transcribed: `while n % 17 != 5: n += 1`.

    Written as the loop upstream writes it rather than as the closed form, so the two agree because
    they compute the same thing and not because both were derived from the same simplification.
    """

    while n % 17 != 5:
        n += 1
    return n


def native_video_latent_t(frame_count: int) -> int:
    """Transcribed: `2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2`."""

    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def native_temporal_shape(nominal_length: int) -> tuple[int, int, int]:
    """Transcribed `temporal_shape`, including its `max(5, ...)` floor and its `round()`.

    The audio term is the origin of the rounded tail for every run whose frame-to-audio mapping is
    non-integral, so it is reproduced in the node's own binary floating point rather than in exact
    arithmetic. Whether that float agrees with the exact rational is a claim `M20-01` has to earn
    over the whole range, not one this fixture may assume.
    """

    frame_count = native_align_frame_count(max(5, nominal_length))
    duration = frame_count / NATIVE_FPS
    audio_latent_t = round(duration * NATIVE_AUDIO_LATENT_FPS)
    return frame_count, native_video_latent_t(frame_count), audio_latent_t
