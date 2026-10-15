"""Explicit harness-only canaries for the continuation route study.

These nodes exist only when ``H3_CONTEXT_HOST_SEQUENCE_CONTINUATION_CANARY=1`` and an explicitly
owned root is supplied in ``H3_CONTEXT_SEQUENCE_CONTINUATION_CANARY_ROOT``. They are absent from
normal registration, carry no product contract, and let the study harness build each route's
successor graph from official host nodes plus these small instruments:

* **LatentTailCapture** keeps the last 12 video tokens and 65 audio steps of a sampled LATENT.
* **TailCompose** applies one latent route to a successor: R-K1 or R-K12 (keyframes on
  ``positive`` only), R-L (the tail written into the latent head under a preserving mask), R-C
  (both), and the taper variants of R-L and R-C.
* **TailWindow** captures the shown-end window of decoded IMAGE and AUDIO (R-G/L).
* **TailClip** returns a stored window as IMAGE and AUDIO for the official ``MiniMaxH3AddGuide``.
* **HeadTrim** trims the head and tail of decoded IMAGE and AUDIO exactly, and may retain the
  untrimmed decode for the harness's pin fidelity and offset sweep.
* **LayoutProbe** reads the host's packed layout on CPU stub tensors (the behavioural fact).

Storage. The study does not use the receipt store: its authority is built from the digest-
pinned temporal profile, and the supplied host's H3 node source no longer has that digest. The
canary instead keeps raw little-endian buffers with a JSON header of exact shape, dtype and SHA-256
under its owned root, verifies all three on every read, and the harness deletes the root's
contents at the end of the run. Nothing here is a product path; the product's continuation store is
a later item's and must not depend on a host digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from .adapters import comfyui_sequence_continuation as host

CANARY_FLAG: Final = "H3_CONTEXT_HOST_SEQUENCE_CONTINUATION_CANARY"
CANARY_ROOT_VARIABLE: Final = "H3_CONTEXT_SEQUENCE_CONTINUATION_CANARY_ROOT"
CANARY_MARKER_NAME: Final = ".h3-context-owned-sequence-continuation-canary"
CANARY_MARKER_PAYLOAD: Final = "h3-context-sequence-continuation-canary/1\n"

_PREFIX: Final = "comfyui_h3_context.H3Context.SequenceContinuation"
LATENT_TAIL_CAPTURE_NODE_ID: Final = _PREFIX + "LatentTailCaptureCanary"
TAIL_COMPOSE_NODE_ID: Final = _PREFIX + "TailComposeCanary"
TAIL_WINDOW_NODE_ID: Final = _PREFIX + "TailWindowCanary"
TAIL_CLIP_NODE_ID: Final = _PREFIX + "TailClipCanary"
HEAD_TRIM_NODE_ID: Final = _PREFIX + "HeadTrimCanary"
LAYOUT_PROBE_NODE_ID: Final = _PREFIX + "LayoutProbeCanary"

#: The pin window: 39 frames, 12 video tokens, 65 audio steps, 52,000 samples at 32 kHz.
PIN_FRAMES: Final = 39
PIN_TOKENS: Final = 12
PIN_AUDIO_STEPS: Final = 65
SAMPLE_RATE: Final = 32_000
FPS: Final = 24
#: The host's per-token frame cycle (``comfy.ldm.minimax.model.FRAME_PER_TOKEN``).
FRAME_PER_TOKEN: Final = (1, 4, 4, 4, 4)
#: The taper variant: the last four pinned tokens are partly regenerated, rising from 0.10.
TAPER_TOKENS: Final = 4
TAPER_START: Final = 0.10
MODES: Final = ("r_k1", "r_k12", "r_l", "r_c", "r_l_taper", "r_c_taper")
_KEY: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_MAX_HEADER_BYTES: Final = 4096
_MAX_BUFFER_BYTES: Final = 512 * 1024 * 1024


class SequenceContinuationCanaryError(ValueError):
    """The canary lane is not enabled or owned, or a stored buffer does not verify."""


def _owned_root() -> Path:
    if os.environ.get(CANARY_FLAG) != "1":
        raise SequenceContinuationCanaryError("sequence continuation canary is not enabled")
    value = os.environ.get(CANARY_ROOT_VARIABLE)
    if not value:
        raise SequenceContinuationCanaryError("sequence continuation canary root is not configured")
    root = Path(value)
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SequenceContinuationCanaryError("canary root is unavailable") from exc
    marker = resolved / CANARY_MARKER_NAME
    if root.is_symlink() or not resolved.is_dir() or not marker.is_file():
        raise SequenceContinuationCanaryError("canary root is not explicitly owned")
    if marker.read_text(encoding="utf-8") != CANARY_MARKER_PAYLOAD:
        raise SequenceContinuationCanaryError("canary ownership marker is invalid")
    return resolved


def _key(value: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise SequenceContinuationCanaryError("buffer key is invalid")
    return value


# --- raw buffer store ------------------------------------------------------------------------


def write_buffer(root: Path, kind: str, key: str, name: str, tensor: Any) -> dict[str, Any]:
    """Write one contiguous CPU tensor as raw bytes plus a verifying header; never overwrite."""

    folder = root / kind / _key(key)
    folder.mkdir(parents=True, exist_ok=True)
    value = tensor.detach().to("cpu").contiguous()
    raw = value.numpy().tobytes()
    if len(raw) > _MAX_BUFFER_BYTES:
        raise SequenceContinuationCanaryError("buffer exceeds the canary bound")
    header = {
        "shape": list(value.shape),
        "dtype": str(value.dtype).removeprefix("torch."),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if header["dtype"] not in {"float32", "uint8"}:
        raise SequenceContinuationCanaryError("buffer dtype is not admitted")
    data_path = folder / f"{name}.raw"
    header_path = folder / f"{name}.json"
    with data_path.open("xb") as stream:
        stream.write(raw)
    with header_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(header, sort_keys=True))
    return header


def read_buffer(root: Path, kind: str, key: str, name: str) -> Any:
    """Read one stored buffer, verifying its exact size and digest before building a tensor."""

    torch = host.torch_module()
    folder = root / kind / _key(key)
    header_path = folder / f"{name}.json"
    data_path = folder / f"{name}.raw"
    for path in (header_path, data_path):
        if path.is_symlink() or not path.is_file():
            raise SequenceContinuationCanaryError("stored buffer is missing")
    if header_path.stat().st_size > _MAX_HEADER_BYTES:
        raise SequenceContinuationCanaryError("stored buffer header is oversized")
    header = json.loads(header_path.read_text(encoding="utf-8"))
    raw = data_path.read_bytes()
    if len(raw) != header["bytes"] or hashlib.sha256(raw).hexdigest() != header["sha256"]:
        raise SequenceContinuationCanaryError("stored buffer does not verify")
    dtype = {"float32": torch.float32, "uint8": torch.uint8}[header["dtype"]]
    flat = torch.frombuffer(bytearray(raw), dtype=dtype)
    return flat.reshape(header["shape"]).clone()


# --- pure tensor helpers (testable without the host) -----------------------------------------


def token_frame_starts(tokens: int = PIN_TOKENS) -> list[int]:
    """Frame index of each token's first frame in a window that starts on a cycle boundary."""

    starts = []
    frame = 0
    for index in range(tokens):
        starts.append(frame)
        frame += FRAME_PER_TOKEN[index % 5]
    return starts


def audio_step_at_frame(frame: int) -> int:
    """The audio step aligned with a pixel frame (40 steps per second, 24 frames per second)."""

    return round(frame * 5 / 3)


def tail_of(video: Any, audio: Any) -> tuple[Any, Any]:
    """The last 12 video tokens and 65 audio steps, cloned."""

    if video.ndim != 5 or video.shape[2] < PIN_TOKENS or audio.shape[-1] < PIN_AUDIO_STEPS:
        raise SequenceContinuationCanaryError("latent is too short for the pin window")
    return video[:, :, -PIN_TOKENS:].clone(), audio[..., -PIN_AUDIO_STEPS:].clone()


def mask_values(tokens: int, steps: int, *, taper: bool) -> tuple[list[float], list[float]]:
    """Per-token video and per-step audio mask values: 0 preserves, 1 generates."""

    if tokens < PIN_TOKENS or steps < PIN_AUDIO_STEPS:
        raise SequenceContinuationCanaryError("latent is too short for the pin window")
    video = [0.0] * PIN_TOKENS + [1.0] * (tokens - PIN_TOKENS)
    audio = [0.0] * PIN_AUDIO_STEPS + [1.0] * (steps - PIN_AUDIO_STEPS)
    if taper:
        starts = token_frame_starts()
        for offset in range(TAPER_TOKENS):
            token = PIN_TOKENS - TAPER_TOKENS + offset
            value = TAPER_START + (1.0 - TAPER_START) * offset / TAPER_TOKENS
            video[token] = value
            first = audio_step_at_frame(starts[token])
            last = (
                PIN_AUDIO_STEPS
                if token + 1 == PIN_TOKENS
                else audio_step_at_frame(starts[token + 1])
            )
            for step in range(first, last):
                audio[step] = value
    return video, audio


def write_head(video: Any, audio: Any, tail_video: Any, tail_audio: Any) -> tuple[Any, Any]:
    """Copies of the anchor latent domains with the tail written into the head."""

    if tail_video.shape[:2] != video.shape[:2] or tail_video.shape[3:] != video.shape[3:]:
        raise SequenceContinuationCanaryError("tail geometry differs from the successor")
    if tail_audio.shape[:3] != audio.shape[:3]:
        raise SequenceContinuationCanaryError("tail audio layout differs from the successor")
    out_video = video.clone()
    out_audio = audio.clone()
    out_video[:, :, :PIN_TOKENS] = tail_video.to(out_video.dtype)
    out_audio[..., :PIN_AUDIO_STEPS] = tail_audio.to(out_audio.dtype)
    return out_video, out_audio


def mask_tensors(video: Any, audio: Any, *, taper: bool) -> tuple[Any, Any]:
    """Dense video ``(1, 1, T, H, W)`` and audio ``(1, 1, 2, L)`` float32 masks."""

    torch = host.torch_module()
    tokens, steps = video.shape[2], audio.shape[-1]
    video_values, audio_values = mask_values(tokens, steps, taper=taper)
    video_mask = (
        torch.tensor(video_values, dtype=torch.float32)
        .reshape(1, 1, tokens, 1, 1)
        .expand(1, 1, tokens, video.shape[3], video.shape[4])
        .contiguous()
    )
    audio_mask = (
        torch.tensor(audio_values, dtype=torch.float32)
        .reshape(1, 1, 1, steps)
        .expand(1, 1, audio.shape[2], steps)
        .contiguous()
    )
    return video_mask, audio_mask


def keyframes_for(mode: str, tail_video: Any, tail_audio: Any) -> list[dict[str, Any]]:
    """The ``minimax_keyframes`` entries a keyframe route adds at frame index 0."""

    if mode in {"r_k1", "r_c", "r_c_taper"}:
        return [{"resolved_frame_index": 0, "latent": tail_video, "audio_latent": tail_audio}]
    if mode == "r_k12":
        # IMPORTANT: the audio stays one 65-step block on the frame-0 entry. The host places an
        # entry's audio rows at ``cursor + 5/3 * frame + j``; split per token, every entry whose
        # start frame is not a multiple of 3 would land up to a third of a step off the target
        # audio rows, and R-K12 would be measured with a misaligned audio anchor.
        entries: list[dict[str, Any]] = []
        for token, frame in enumerate(token_frame_starts()):
            entry: dict[str, Any] = {
                "resolved_frame_index": frame,
                "latent": tail_video[:, :, token : token + 1].clone(),
            }
            if token == 0:
                entry["audio_latent"] = tail_audio
            entries.append(entry)
        return entries
    return []


def trim_extents(trim_frames: int, keep_frames: int) -> tuple[int, int]:
    """Sample offsets for a frame trim: ``round(frames * 4000 / 3)`` at 32 kHz."""

    if trim_frames < 0 or keep_frames <= 0:
        raise SequenceContinuationCanaryError("trim extents are invalid")
    return round(trim_frames * 4000 / 3), keep_frames * SAMPLE_RATE // FPS


# --- host-facing nodes -----------------------------------------------------------------------


def _latent_domains(latent: dict[str, Any]) -> tuple[Any, Any]:
    samples = latent.get("samples") if isinstance(latent, dict) else None
    tensors = getattr(samples, "tensors", None)
    if not getattr(samples, "is_nested", False) or tensors is None or len(tensors) != 2:
        raise SequenceContinuationCanaryError("expected a MiniMax H3 AV latent")
    return tensors[0], tensors[1]


class H3SequenceContinuationLatentTailCaptureCanaryNode:
    """Store the last 12 video tokens and 65 audio steps of a sampled latent."""

    __h3_context_node_id__ = LATENT_TAIL_CAPTURE_NODE_ID
    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {"required": {"samples": ("LATENT",), "key": ("STRING", {"default": ""})}}

    def execute(self, samples: dict[str, Any], key: str) -> dict[str, Any]:
        root = _owned_root()
        video, audio = _latent_domains(samples)
        tail_video, tail_audio = tail_of(video.float(), audio.float())
        headers = {
            "video": write_buffer(root, "latent-tails", key, "video", tail_video),
            "audio": write_buffer(root, "latent-tails", key, "audio", tail_audio),
        }
        return {"ui": {"latent_tail": [json.dumps(headers, sort_keys=True)]}, "result": (samples,)}


class H3SequenceContinuationTailComposeCanaryNode:
    """Apply one latent route's change to the successor's ``positive`` and anchor LATENT."""

    __h3_context_node_id__ = TAIL_COMPOSE_NODE_ID
    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "latent")
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {
            "required": {
                "positive": ("CONDITIONING",),
                "latent": ("LATENT",),
                "key": ("STRING", {"default": ""}),
                "mode": (list(MODES),),
            }
        }

    def execute(
        self, positive: Any, latent: dict[str, Any], key: str, mode: str
    ) -> tuple[Any, dict[str, Any]]:
        if mode not in MODES:
            raise SequenceContinuationCanaryError("unknown route mode")
        root = _owned_root()
        tail_video = read_buffer(root, "latent-tails", key, "video")
        tail_audio = read_buffer(root, "latent-tails", key, "audio")
        video, audio = _latent_domains(latent)
        out_latent = latent
        if mode in {"r_l", "r_c", "r_l_taper", "r_c_taper"}:
            new_video, new_audio = write_head(video, audio, tail_video, tail_audio)
            video_mask, audio_mask = mask_tensors(video, audio, taper=mode.endswith("_taper"))
            out_latent = dict(latent)
            out_latent["samples"] = host.nested_tensor((new_video, new_audio))
            out_latent["noise_mask"] = host.nested_tensor((video_mask, audio_mask))
        entries = keyframes_for(mode, tail_video, tail_audio)
        if entries:
            existing = list(positive[0][1].get("minimax_keyframes", []))
            positive = host.conditioning_set_values(
                positive, {"minimax_keyframes": existing + entries}
            )
        return positive, out_latent


def _image_to_uint8(images: Any) -> Any:
    return (
        (images.detach().float().clamp(0.0, 1.0) * 255.0)
        .round()
        .to("cpu")
        .to(host.torch_module().uint8)
    )


class H3SequenceContinuationTailWindowCanaryNode:
    """Capture the shown-end window ``[shown - 39, shown)`` and its exact audio (R-G/L)."""

    __h3_context_node_id__ = TAIL_WINDOW_NODE_ID
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "key": ("STRING", {"default": ""}),
                "shown_frames": ("INT", {"default": 153, "min": PIN_FRAMES, "max": 4096}),
            }
        }

    def execute(
        self, images: Any, audio: dict[str, Any], key: str, shown_frames: int
    ) -> dict[str, Any]:
        root = _owned_root()
        if audio.get("sample_rate") != SAMPLE_RATE or (shown_frames * SAMPLE_RATE) % FPS:
            raise SequenceContinuationCanaryError("window audio is not sample-exact")
        if images.shape[0] < shown_frames:
            raise SequenceContinuationCanaryError("window is beyond the shown content")
        end_sample = shown_frames * SAMPLE_RATE // FPS
        start_sample = end_sample - PIN_FRAMES * SAMPLE_RATE // FPS
        waveform = audio["waveform"]
        if waveform.shape[-1] < end_sample:
            raise SequenceContinuationCanaryError("window audio is beyond the decoded audio")
        frames = _image_to_uint8(images[shown_frames - PIN_FRAMES : shown_frames])
        headers = {
            "frames": write_buffer(root, "windows", key, "frames", frames),
            "audio": write_buffer(
                root, "windows", key, "audio", waveform[0, :, start_sample:end_sample].float()
            ),
        }
        return {
            "ui": {"tail_window": [json.dumps(headers, sort_keys=True)]},
            "result": (images, audio),
        }


class H3SequenceContinuationTailClipCanaryNode:
    """A stored 39-frame window as IMAGE and its 52,000 samples as AUDIO."""

    __h3_context_node_id__ = TAIL_CLIP_NODE_ID
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {"required": {"key": ("STRING", {"default": ""})}}

    def execute(self, key: str) -> tuple[Any, dict[str, Any]]:
        root = _owned_root()
        frames = read_buffer(root, "windows", key, "frames")
        audio = read_buffer(root, "windows", key, "audio")
        if frames.shape[0] != PIN_FRAMES or audio.shape[-1] != PIN_FRAMES * SAMPLE_RATE // FPS:
            raise SequenceContinuationCanaryError("stored window extents are wrong")
        images = frames.float() / 255.0
        return images, {"waveform": audio.unsqueeze(0), "sample_rate": SAMPLE_RATE}


class H3SequenceContinuationHeadTrimCanaryNode:
    """Trim ``trim_frames`` from the head and keep ``keep_frames``, exactly in both domains."""

    __h3_context_node_id__ = HEAD_TRIM_NODE_ID
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {
            "required": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "trim_frames": ("INT", {"default": PIN_FRAMES, "min": 0, "max": 4096}),
                "keep_frames": ("INT", {"default": 153, "min": 1, "max": 4096}),
                "retain_key": ("STRING", {"default": ""}),
            }
        }

    def execute(
        self,
        images: Any,
        audio: dict[str, Any],
        trim_frames: int,
        keep_frames: int,
        retain_key: str,
    ) -> tuple[Any, dict[str, Any]]:
        if audio.get("sample_rate") != SAMPLE_RATE:
            raise SequenceContinuationCanaryError("decoded audio is not 32 kHz")
        start_sample, keep_samples = trim_extents(trim_frames, keep_frames)
        waveform = audio["waveform"]
        if images.shape[0] < trim_frames + keep_frames:
            raise SequenceContinuationCanaryError("too few decoded frames for the trim")
        if waveform.shape[-1] < start_sample + keep_samples:
            raise SequenceContinuationCanaryError("too few decoded samples for the trim")
        if retain_key:
            root = _owned_root()
            write_buffer(root, "untrimmed", retain_key, "frames", _image_to_uint8(images))
            write_buffer(root, "untrimmed", retain_key, "audio", waveform[0].float())
        trimmed_images = images[trim_frames : trim_frames + keep_frames]
        trimmed_audio = {
            "waveform": waveform[..., start_sample : start_sample + keep_samples],
            "sample_rate": SAMPLE_RATE,
        }
        return trimmed_images, trimmed_audio


def layout_facts(model: Any, torch: Any) -> dict[str, Any]:
    """The host's packed-layout behaviour on CPU stub tensors (no GPU, no weights)."""

    text_len, latent_t, latent_h, latent_w, audio_t = 4, 57, 4, 6, 320
    rows = (latent_h // 2) * (latent_w // 2)
    span = model.FRAME_RESCALE

    def layout(keyframes: Sequence[dict[str, Any]], refs: Sequence[dict[str, Any]] = ()) -> Any:
        return model.PackedLayout(
            text_len,
            latent_t,
            latent_h,
            latent_w,
            audio_t,
            keyframes=list(keyframes),
            refs=list(refs),
        )

    stub_video = torch.zeros(1, 24, PIN_TOKENS, latent_h, latent_w)
    stub_audio = torch.zeros(1, 32, 2, PIN_AUDIO_STEPS)
    facts: dict[str, Any] = {
        "frame_per_token": list(model.FRAME_PER_TOKEN),
        "frame_rescale": float(span),
    }
    index = 17
    base = layout([{"resolved_frame_index": index, "latent": stub_video}])
    keyframe_t = float(base.position_ids[text_len, 0])
    facts["keyframe_at_cursor_plus_rescaled_index"] = (
        abs(keyframe_t - (text_len + span * index)) < 1e-9
    )
    with_ref = layout(
        [{"resolved_frame_index": index, "latent": stub_video}],
        [{"kind": "image", "latent_h": latent_h, "latent_w": latent_w}],
    )
    shifted_t = float(with_ref.position_ids[text_len, 0])
    facts["reference_moves_cursor_not_offset"] = abs(shifted_t - (keyframe_t + 1.0)) < 1e-9
    audio_layout = layout([{"resolved_frame_index": 0, "audio_latent": stub_audio}])
    kinds = [kind for _, _, kind in audio_layout.segments]
    facts["audio_keyframe_yields_cond_audio"] = "cond_audio" in kinds
    one_token = layout([{"resolved_frame_index": 5, "latent": stub_video[:, :, :1]}])
    cond = [(start, stop) for start, stop, kind in one_token.segments if kind == "cond"]
    facts["one_step_entry_occupies_one_token"] = len(cond) == 1 and cond[0][1] - cond[0][0] == rows
    facts["layout_behaviour"] = all(
        facts[name]
        for name in (
            "keyframe_at_cursor_plus_rescaled_index",
            "reference_moves_cursor_not_offset",
            "audio_keyframe_yields_cond_audio",
            "one_step_entry_occupies_one_token",
        )
    )
    return facts


class H3SequenceContinuationLayoutProbeCanaryNode:
    """Report the behavioural layout facts the routes depend on."""

    __h3_context_node_id__ = LAYOUT_PROBE_NODE_ID
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("facts",)
    FUNCTION = "execute"
    CATEGORY = "h3_context/internal"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[object, ...]]]:
        return {"required": {}}

    def execute(self) -> dict[str, Any]:
        _owned_root()
        torch = host.torch_module()
        model = host.minimax_model_module()
        wire = json.dumps(layout_facts(model, torch), sort_keys=True)
        return {"ui": {"facts": [wire]}, "result": (wire,)}


CANARY_NODES: Final[dict[str, type[Any]]] = {
    LATENT_TAIL_CAPTURE_NODE_ID: H3SequenceContinuationLatentTailCaptureCanaryNode,
    TAIL_COMPOSE_NODE_ID: H3SequenceContinuationTailComposeCanaryNode,
    TAIL_WINDOW_NODE_ID: H3SequenceContinuationTailWindowCanaryNode,
    TAIL_CLIP_NODE_ID: H3SequenceContinuationTailClipCanaryNode,
    HEAD_TRIM_NODE_ID: H3SequenceContinuationHeadTrimCanaryNode,
    LAYOUT_PROBE_NODE_ID: H3SequenceContinuationLayoutProbeCanaryNode,
}
CANARY_DISPLAY_NAMES: Final = {
    LATENT_TAIL_CAPTURE_NODE_ID: "H3 Context (continuation latent tail capture canary)",
    TAIL_COMPOSE_NODE_ID: "H3 Context (continuation tail compose canary)",
    TAIL_WINDOW_NODE_ID: "H3 Context (continuation tail window canary)",
    TAIL_CLIP_NODE_ID: "H3 Context (continuation tail clip canary)",
    HEAD_TRIM_NODE_ID: "H3 Context (continuation head trim canary)",
    LAYOUT_PROBE_NODE_ID: "H3 Context (continuation layout probe canary)",
}
