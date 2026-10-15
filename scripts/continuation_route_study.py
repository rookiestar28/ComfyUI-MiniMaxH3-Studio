"""Continuation route study harness: generate the fixed corpus on the supplied host, then measure.

This is a study instrument, not a product path. It POSTs graphs built from the official H3 nodes
plus the env-gated sequence continuation canaries to an already running supplied host, records
content-free facts (extents, digests, timings, peak memory, metrics), and keeps every decoded
buffer in a private work directory that is deleted at the end of the study.

Subcommands:

* ``generate``: run every corpus generation that has no completed receipt yet (resumable).
* ``measure``: compute S1-S12 per chain and seam from the outputs and retained buffers.
* ``sheets``: write contact sheets of seam +/- 6 frames for each chain (generated content only).

The fixed corpus, routes and selection rule are those of the finalized plan; nothing here
chooses a route. ``decide`` in the record applies the rule to ``measure``'s output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comfyui_h3_context.adapters.seam_observation_media import (  # noqa: E402
    SeamObservationDecoder,
    scaled_height,
)
from comfyui_h3_context.core import seam_observation as so  # noqa: E402
from scripts.m19_08.runs import (  # noqa: E402
    ProbeError,
    WeightDesignation,
    assert_queue_idle,
    queue_prompt,
    wait_for_history,
)

SCHEMA: Final = "h3-context-continuation-route-study/1"
WIDTH: Final = 864
HEIGHT: Final = 480
STEPS: Final = 20
FPS: Final = 24
SAMPLE_RATE: Final = 32_000
METRIC_RATE: Final = 16_000
FIRST_FRAMES: Final = 192
SHOWN_FRAMES: Final = 153
CONTINUATION_FRAMES: Final = 192
CUT_FRAMES: Final = 158
PIN: Final = 39
SEEDS: Final = (1, 2, 3)
RUN_TIMEOUT_SECONDS: Final = 3600.0
DESIGNATION: Final = WeightDesignation(
    video_model="H3\\minimax_h3_fl2va_bf16.safetensors",
    text_encoder="qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    video_vae="minimax_h3_video_vae_fp16.safetensors",
    audio_vae="minimax_h3_audio_vae_fp32.safetensors",
)
_CANARY: Final = "comfyui_h3_context.H3Context.SequenceContinuation"

#: Arm id -> (route kind, canary mode). Arm ids are canary keys, so they use hyphens only.
ARMS: Final = {
    "cut": ("cut", ""),
    "rg-l": ("guide", ""),
    "rg-h": ("guide", ""),
    "rk1": ("latent", "r_k1"),
    "rk12": ("latent", "r_k12"),
    "rl": ("latent", "r_l"),
    "rc": ("latent", "r_c"),
    "rl-taper": ("latent", "r_l_taper"),
    "rc-taper": ("latent", "r_c_taper"),
}
BASE_ARMS: Final = ("cut", "rg-l", "rg-h", "rk1", "rl", "rc")
MOTION_ONLY_ARMS: Final = ("rk12",)

_STYLE: Final = (
    "Realistic live-action cinematic footage, natural light, shallow depth of field. "
    "No text, no subtitles, no logos, no watermarks."
)
PROMPTS: Final = {
    "motion": (
        "A red vintage convertible drives along a winding coastal road at golden hour. The camera "
        "tracks alongside the car in one continuous, smooth move; waves break on the dark rocks "
        "below and palm trees slide past. The shot never cuts. Audio: steady engine hum, tyres on "
        "asphalt, wind and distant surf, no music. " + _STYLE,
        "The same continuous tracking shot carries on without a cut: the red vintage convertible "
        "is already driving along the coastal road at golden hour and keeps going, the camera "
        "keeps pace beside it, and the road bends gently left towards a white lighthouse on the "
        "headland. Audio: the same engine hum, tyres, wind and surf continue, no music. " + _STYLE,
        "The same continuous tracking shot carries on without a cut: the red vintage convertible "
        "is already driving and keeps going past the white lighthouse, the camera still beside "
        "it, the sun lower and warmer over the sea. Audio: the same engine hum, tyres, wind and "
        "surf continue, no music. " + _STYLE,
    ),
    # Supplementary segment 3 for C-motion: the base corpus's segment-3 prompt asks for "the sun
    # lower and warmer", which requests the very colour change S4 measures. This variant keeps
    # segments 1 and 2 of C-motion and only restates segment 3 with the light unchanged.
    "motionb": (
        "",
        "",
        "The same continuous tracking shot carries on without a cut: the red vintage convertible "
        "is already driving and keeps going past the white lighthouse, the camera still beside "
        "it, in the same golden-hour light. Audio: the same engine hum, tyres, wind and surf "
        "continue, no music. " + _STYLE,
    ),
    "dialogue": (
        "Two friends sit at a small cafe table by a rain-streaked window in soft afternoon "
        "light: a woman with short black hair in a green sweater, and a man with a grey beard "
        "in a navy jacket. A steady medium two-shot with a very slow push-in; the shot never "
        'cuts. [1s-3.5s] The woman says: "Did you bring the map?" [4.5s-7s] The man answers: '
        '"It\'s in my bag, right here." Audio: clear dialogue, quiet cafe murmur, a cup set '
        "down, no music. " + _STYLE,
        "The same steady medium two-shot carries on without a cut: the two friends are still "
        "at the cafe table by the rain-streaked window, the man is already reaching into his bag "
        "and unfolding a paper map while the woman leans in to look. [{t0}s-{t1}s] The woman "
        'says: "Then let\'s leave before the rain gets worse." Audio: clear dialogue, quiet '
        "cafe murmur, paper rustling, no music. " + _STYLE,
        "The same steady medium two-shot carries on without a cut: the two friends are still "
        "at the cafe table, the paper map already spread between them, the man tracing a route "
        'with his finger. [{t0}s-{t1}s] The man says: "We take the coast road, then." Audio: '
        "clear dialogue, quiet cafe murmur, rain on the window, no music. " + _STYLE,
    ),
}
#: The line sits 2.5 s into the shown time. A continuation segment states it on its generated
#: timeline, 39 / 24 = 1.625 s later (D-07); a cut segment shows its generated time unchanged.
LINE_SHOWN_START_S: Final = 2.5
#: A prompt variant that only restates later segments reuses its parent's earlier segments.
VARIANT_PARENT: Final = {"motionb": "motion"}
LINE_DURATION_S: Final = 2.5


class StudyError(RuntimeError):
    """A study precondition or a run failed; the receipt records the reason."""


@dataclass(frozen=True)
class Generation:
    """One corpus generation: a chain's segment under one arm."""

    prompt: str
    seed: int
    arm: str
    segment: int

    @property
    def key(self) -> str:
        return f"{self.prompt}-{self.seed}-{self.arm}-{self.segment}"

    @property
    def predecessor(self) -> Generation | None:
        if self.segment == 1:
            return None
        parent = VARIANT_PARENT.get(self.prompt, self.prompt)
        if self.segment == 2:
            return Generation(parent, self.seed, "base", 1)
        if self.segment == 3:
            return Generation(parent, self.seed, self.arm, 2)
        return Generation(self.prompt, self.seed, self.arm, self.segment - 1)

    @property
    def generated_frames(self) -> int:
        if self.segment == 1:
            return FIRST_FRAMES
        return CUT_FRAMES if self.arm == "cut" else CONTINUATION_FRAMES

    @property
    def trim_frames(self) -> int:
        return 0 if self.segment == 1 or self.arm == "cut" else PIN

    @property
    def shown_frames(self) -> int:
        return FIRST_FRAMES if self.segment == 1 else SHOWN_FRAMES

    @property
    def captures_latent_tail(self) -> bool:
        return self.segment == 1 or (self.segment == 2 and ARMS[self.arm][0] == "latent")

    @property
    def captures_window(self) -> bool:
        return self.segment == 1 or (self.segment == 2 and self.arm == "rg-l")

    def prompt_text(self) -> str:
        text = PROMPTS[self.prompt][self.segment - 1]
        if "{t0}" not in text:
            return text
        start = LINE_SHOWN_START_S + self.trim_frames / FPS
        return text.format(t0=_seconds(start), t1=_seconds(start + LINE_DURATION_S))

    @property
    def noise_seed(self) -> int:
        # Every arm of a (prompt, seed) shares a segment's noise, so arms differ only in route.
        return self.seed * 100 + self.segment


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def corpus(arms_by_prompt: dict[str, Sequence[str]]) -> list[Generation]:
    """Every generation in run order: per (prompt, seed), segment 1 then each arm's 2 and 3."""

    items: list[Generation] = []
    for prompt, arms in arms_by_prompt.items():
        for seed in SEEDS:
            items.append(Generation(prompt, seed, "base", 1))
            for arm in arms:
                items.append(Generation(prompt, seed, arm, 2))
                items.append(Generation(prompt, seed, arm, 3))
    return items


def base_corpus() -> list[Generation]:
    return corpus(
        {"motion": (*BASE_ARMS, *MOTION_ONLY_ARMS), "dialogue": BASE_ARMS},
    )


# --- graphs ----------------------------------------------------------------------------------


def build_graph(item: Generation, *, prefix: str) -> dict[str, Any]:
    """The official template's guidance-free topology plus this arm's canary nodes."""

    graph: dict[str, Any] = {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": DESIGNATION.video_model, "weight_dtype": "default"},
        },
        "c": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": DESIGNATION.text_encoder,
                "type": "minimax",
                "device": "default",
            },
        },
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": DESIGNATION.video_vae}},
        "av": {"class_type": "VAELoader", "inputs": {"vae_name": DESIGNATION.audio_vae}},
        "h3": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["c", 0],
                "vae": ["vv", 0],
                "prompt": item.prompt_text(),
                "width": WIDTH,
                "height": HEIGHT,
                "length": item.generated_frames,
            },
        },
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "sigmas": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["u", 0], "scheduler": "simple", "steps": STEPS, "denoise": 1},
        },
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": item.noise_seed}},
    }
    positive: list[Any] = ["h3", 0]
    latent: list[Any] = ["h3", 1]
    kind, mode = ARMS.get(item.arm, ("cut", ""))
    predecessor = item.predecessor
    if item.segment > 1 and kind == "latent" and predecessor is not None:
        graph["route"] = {
            "class_type": _CANARY + "TailComposeCanary",
            "inputs": {
                "positive": positive,
                "latent": latent,
                "key": predecessor.key,
                "mode": mode,
            },
        }
        positive, latent = ["route", 0], ["route", 1]
    elif item.segment > 1 and kind == "guide" and predecessor is not None:
        graph["clip"] = {
            "class_type": _CANARY + "TailClipCanary",
            "inputs": {"key": _window_key(predecessor, item.arm)},
        }
        graph["route"] = {
            "class_type": "MiniMaxH3AddGuide",
            "inputs": {
                "positive": positive,
                "vae": ["vv", 0],
                "audio_vae": ["av", 0],
                "latent": latent,
                "image": ["clip", 0],
                "audio": ["clip", 1],
                "frame_idx": 0,
            },
        }
        positive = ["route", 0]
    graph["guider"] = {
        "class_type": "BasicGuider",
        "inputs": {"model": ["u", 0], "conditioning": positive},
    }
    graph["run"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["noise", 0],
            "guider": ["guider", 0],
            "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0],
            "latent_image": latent,
        },
    }
    sampled: list[Any] = ["run", 0]
    if item.captures_latent_tail:
        graph["capture"] = {
            "class_type": _CANARY + "LatentTailCaptureCanary",
            "inputs": {"samples": sampled, "key": item.key},
        }
        sampled = ["capture", 0]
    graph["decode_v"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": sampled, "vae": ["vv", 0]},
    }
    graph["decode_a"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": sampled, "vae": ["av", 0]},
    }
    graph["trim"] = {
        "class_type": _CANARY + "HeadTrimCanary",
        "inputs": {
            "images": ["decode_v", 0],
            "audio": ["decode_a", 0],
            "trim_frames": item.trim_frames,
            "keep_frames": item.shown_frames,
            "retain_key": item.key,
        },
    }
    shown_images: list[Any] = ["trim", 0]
    shown_audio: list[Any] = ["trim", 1]
    if item.captures_window:
        graph["window"] = {
            "class_type": _CANARY + "TailWindowCanary",
            "inputs": {
                "images": shown_images,
                "audio": shown_audio,
                "key": item.key,
                "shown_frames": item.shown_frames,
            },
        }
        shown_images, shown_audio = ["window", 0], ["window", 1]
    if item.segment == 1:
        # The double-decode floor: a second decode of the same latent through a different
        # upstream link, so the host cannot serve it from its output cache.
        graph["decode_v2"] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["run", 0], "vae": ["vv", 0]},
        }
        graph["decode_a2"] = {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["run", 0], "vae": ["av", 0]},
        }
        graph["trim2"] = {
            "class_type": _CANARY + "HeadTrimCanary",
            "inputs": {
                "images": ["decode_v2", 0],
                "audio": ["decode_a2", 0],
                "trim_frames": 0,
                "keep_frames": item.shown_frames,
                "retain_key": item.key + "-dd",
            },
        }
    graph["video"] = {
        "class_type": "CreateVideo",
        "inputs": {"images": shown_images, "audio": shown_audio, "fps": float(FPS)},
    }
    graph["save"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["video", 0],
            "filename_prefix": f"{prefix}/{item.key}",
            "format": "mp4",
            "format.codec": "h264",
        },
    }
    return graph


def _window_key(predecessor: Generation, arm: str) -> str:
    """R-G/L reads the in-graph window; R-G/H reads the window the harness decoded from MP4."""

    return predecessor.key if arm == "rg-l" else predecessor.key + "-h264"


# --- private buffers -------------------------------------------------------------------------


def _read_buffer(root: Path, kind: str, key: str, name: str) -> tuple[list[int], str, bytes]:
    header = json.loads((root / kind / key / f"{name}.json").read_text(encoding="utf-8"))
    raw = (root / kind / key / f"{name}.raw").read_bytes()
    if len(raw) != header["bytes"] or hashlib.sha256(raw).hexdigest() != header["sha256"]:
        raise StudyError(f"stored buffer {kind}/{key}/{name} does not verify")
    return list(header["shape"]), str(header["dtype"]), raw


def _write_buffer(
    root: Path, kind: str, key: str, name: str, shape: Sequence[int], dtype: str, raw: bytes
) -> None:
    folder = root / kind / key
    folder.mkdir(parents=True, exist_ok=True)
    header = {
        "shape": list(shape),
        "dtype": dtype,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    with (folder / f"{name}.raw").open("xb") as stream:
        stream.write(raw)
    with (folder / f"{name}.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(header, sort_keys=True))


def _remove_keys(root: Path, keys: Sequence[tuple[str, str]]) -> None:
    for kind, key in keys:
        folder = root / kind / key
        if folder.is_dir():
            shutil.rmtree(folder)


def compact_untrimmed(root: Path, key: str, work: Path) -> dict[str, Any]:
    """Reduce a retained untrimmed decode to 96 px luma and 16 kHz mono, then delete it."""

    import torch

    shape, dtype, raw = _read_buffer(root, "untrimmed", key, "frames")
    if dtype != "uint8" or len(shape) != 4 or shape[3] != 3:
        raise StudyError("retained frames have an unexpected layout")
    frames = torch.frombuffer(bytearray(raw), dtype=torch.uint8).reshape(shape).float()
    del raw
    luma = 0.299 * frames[..., 0] + 0.587 * frames[..., 1] + 0.114 * frames[..., 2]
    del frames
    height = scaled_height(shape[2], shape[1], 96)
    small = torch.nn.functional.adaptive_avg_pool2d(luma.unsqueeze(1), (height, 96)).squeeze(1)
    luma_bytes = small.round().clamp(0, 255).to(torch.uint8).contiguous().numpy().tobytes()
    a_shape, a_dtype, a_raw = _read_buffer(root, "untrimmed", key, "audio")
    if a_dtype != "float32" or len(a_shape) != 2:
        raise StudyError("retained audio has an unexpected layout")
    audio = torch.frombuffer(bytearray(a_raw), dtype=torch.float32).reshape(a_shape)
    mono = audio.mean(dim=0)
    usable = mono.shape[0] - mono.shape[0] % 2
    mono16 = mono[:usable].reshape(-1, 2).mean(dim=1)
    pcm = (mono16 * 32767.0).round().clamp(-32768, 32767).to(torch.int16).numpy().tobytes()
    target = work / "compact" / key
    target.mkdir(parents=True, exist_ok=True)
    (target / "luma96.raw").write_bytes(luma_bytes)
    (target / "mono16k.raw").write_bytes(pcm)
    facts = {
        "frames": shape[0],
        "source_height": shape[1],
        "source_width": shape[2],
        "luma_height": height,
        "samples_32k": a_shape[1],
        "channels": a_shape[0],
    }
    (target / "facts.json").write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")
    _remove_keys(root, [("untrimmed", key)])
    return facts


def load_compact(work: Path, key: str) -> tuple[list[bytes], array[int], dict[str, Any]]:
    folder = work / "compact" / key
    facts = json.loads((folder / "facts.json").read_text(encoding="utf-8"))
    raw = (folder / "luma96.raw").read_bytes()
    size = facts["luma_height"] * 96
    if len(raw) != size * facts["frames"]:
        raise StudyError("compact luma has the wrong size")
    frames = [raw[i * size : (i + 1) * size] for i in range(facts["frames"])]
    pcm: array[int] = array("h")
    pcm.frombytes((folder / "mono16k.raw").read_bytes())
    return frames, pcm, facts


# --- media tools -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path

    def run(self, argv: Sequence[str], *, limit: int) -> bytes:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=600, check=False, stdin=subprocess.DEVNULL
        )
        if completed.returncode != 0 or len(completed.stdout) > limit:
            raise StudyError(f"media tool failed with exit {completed.returncode}")
        return completed.stdout


def decode_window(tools: MediaTools, source: Path, *, shown: int) -> tuple[bytes, bytes]:
    """R-G/H: the shown-end window decoded from the committed H.264 artifact, full size."""

    first = shown - PIN
    frame_bytes = WIDTH * HEIGHT * 3
    video = tools.run(
        [
            str(tools.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            f"trim=start_frame={first}:end_frame={shown},setpts=PTS-STARTPTS",
            # IMPORTANT: a trimmed selection must not be re-padded to the source timeline; the
            # default constant-rate output duplicates frames up to the original duration.
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        limit=frame_bytes * PIN,
    )
    if len(video) != frame_bytes * PIN:
        raise StudyError("R-G/H window decode is not exactly 39 frames")
    end = shown * SAMPLE_RATE // FPS
    start = end - PIN * SAMPLE_RATE // FPS
    audio = tools.run(
        [
            str(tools.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-af",
            f"atrim=start_sample={start}:end_sample={end}",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        limit=8 * (end - start),
    )
    if len(audio) != 8 * (end - start):
        raise StudyError("R-G/H window audio is not exactly 52,000 samples")
    # f32le interleaved stereo -> planar (2, N), as the canary store expects.
    samples = array("f")
    samples.frombytes(audio)
    planar = array("f", samples[0::2]) + array("f", samples[1::2])
    return video, planar.tobytes()


def exactness(tools: MediaTools, source: Path, *, frames: int) -> dict[str, Any]:
    """S7: decoded frame count, decoded 32 kHz samples, start 0 and end, from the artifact."""

    probe = json.loads(
        tools.run(
            [
                str(tools.ffprobe),
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=codec_type,nb_read_frames,start_time,width,height,sample_rate",
                "-of",
                "json",
                str(source),
            ],
            limit=1 << 20,
        )
    )
    streams = {s["codec_type"]: s for s in probe["streams"]}
    pcm = tools.run(
        [
            str(tools.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ],
        limit=1 << 26,
    )
    expected_samples = frames * SAMPLE_RATE // FPS
    video, audio = streams["video"], streams["audio"]
    facts = {
        "frames": int(video["nb_read_frames"]),
        "samples": len(pcm) // 2,
        "video_start": float(video["start_time"]),
        "audio_start": float(audio["start_time"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "sample_rate": int(audio["sample_rate"]),
    }
    facts["exact"] = (
        facts["frames"] == frames
        and facts["samples"] == expected_samples
        and facts["video_start"] == 0.0
        and facts["audio_start"] == 0.0
        and (facts["width"], facts["height"]) == (WIDTH, HEIGHT)
        and facts["sample_rate"] == SAMPLE_RATE
    )
    facts["expected_samples"] = expected_samples
    return facts


# --- generation ------------------------------------------------------------------------------


class VramWatch:
    """Poll ``nvidia-smi`` while a run executes; the peak is the device's used memory."""

    def __init__(self) -> None:
        self.peak_mib = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                ).stdout
                self.peak_mib = max(self.peak_mib, int(out.split()[0]))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
            self._stop.wait(1.0)

    def __enter__(self) -> VramWatch:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=15)


def _saved_video(entry: dict[str, Any]) -> dict[str, str]:
    for output in entry.get("outputs", {}).values():
        for value in output.get("images", []) or []:
            if isinstance(value, dict) and str(value.get("filename", "")).endswith(".mp4"):
                return {"filename": value["filename"], "subfolder": value.get("subfolder", "")}
    raise StudyError("the run produced no MP4")


def _receipt_path(work: Path, item: Generation) -> Path:
    return work / "receipts" / f"{item.key}.json"


def _artifact(output_root: Path, receipt: dict[str, Any]) -> Path:
    return output_root / str(receipt["subfolder"]) / str(receipt["filename"])


def generate(args: argparse.Namespace, items: Sequence[Generation]) -> int:
    root, work, output_root = Path(args.canary_root), Path(args.work), Path(args.output_root)
    tools = MediaTools(Path(args.ffmpeg), Path(args.ffprobe))
    (work / "receipts").mkdir(parents=True, exist_ok=True)
    done = 0
    for item in items:
        receipt_path = _receipt_path(work, item)
        if receipt_path.is_file():
            continue
        predecessor = item.predecessor
        if predecessor is not None and not _receipt_path(work, predecessor).is_file():
            raise StudyError(f"{item.key}: predecessor {predecessor.key} has no receipt")
        if item.arm == "rg-h" and predecessor is not None:
            pred_receipt = json.loads(_receipt_path(work, predecessor).read_text(encoding="utf-8"))
            window_key = _window_key(predecessor, "rg-h")
            _remove_keys(root, [("windows", window_key)])
            video, audio = decode_window(
                tools, _artifact(output_root, pred_receipt), shown=predecessor.shown_frames
            )
            _write_buffer(
                root, "windows", window_key, "frames", [PIN, HEIGHT, WIDTH, 3], "uint8", video
            )
            _write_buffer(
                root,
                "windows",
                window_key,
                "audio",
                [2, PIN * SAMPLE_RATE // FPS],
                "float32",
                audio,
            )
        # A resumed run replaces whatever an interrupted attempt of this generation left behind.
        _remove_keys(
            root,
            [
                ("latent-tails", item.key),
                ("windows", item.key),
                ("untrimmed", item.key),
                ("untrimmed", item.key + "-dd"),
            ],
        )
        assert_queue_idle(args.base_url)
        graph = build_graph(item, prefix=args.prefix)
        started = time.monotonic()
        with VramWatch() as watch:
            prompt_id = queue_prompt(args.base_url, graph)
            entry = wait_for_history(args.base_url, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
        wall = time.monotonic() - started
        saved = _saved_video(entry)
        artifact = output_root / saved["subfolder"] / saved["filename"]
        compact = compact_untrimmed(root, item.key, work)
        floor = compact_untrimmed(root, item.key + "-dd", work) if item.segment == 1 else None
        receipt = {
            "key": item.key,
            "prompt": item.prompt,
            "seed": item.seed,
            "noise_seed": item.noise_seed,
            "arm": item.arm,
            "segment": item.segment,
            "generated_frames": item.generated_frames,
            "trim_frames": item.trim_frames,
            "shown_frames": item.shown_frames,
            "wall_seconds": round(wall, 2),
            "peak_vram_mib": watch.peak_mib,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "artifact_bytes": artifact.stat().st_size,
            "untrimmed": compact,
            "double_decode": floor,
            **saved,
        }
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        done += 1
        print(
            json.dumps(
                {
                    "done": item.key,
                    "wall_s": receipt["wall_seconds"],
                    "peak_vram_mib": watch.peak_mib,
                }
            ),
            flush=True,
        )
    return done


# --- measurement -----------------------------------------------------------------------------


def _segment_media(decoder: SeamObservationDecoder, path: Path, frames: int) -> dict[str, Any]:
    luma = decoder.luma_frames(path, source_width=WIDTH, source_height=HEIGHT, frames=frames)
    rgb = decoder.rgb_frames(path, source_width=WIDTH, source_height=HEIGHT, frames=frames)
    audio = decoder.mono_audio(path, samples=frames * METRIC_RATE // FPS)
    return {
        "luma": list(luma.frames),
        "rgb": list(rgb.frames),
        "audio": audio,
        "height": luma.height,
    }


def _chain(prompt: str, seed: int, arm: str) -> list[Generation]:
    parent = VARIANT_PARENT.get(prompt, prompt)
    return [
        Generation(parent, seed, "base", 1),
        Generation(parent, seed, arm, 2),
        Generation(prompt, seed, arm, 3),
    ]


def _round(value: float) -> float:
    return round(value, 6)


def measure_chain(
    args: argparse.Namespace,
    decoder: SeamObservationDecoder,
    tools: MediaTools,
    chain: list[Generation],
) -> dict[str, Any]:
    work, output_root = Path(args.work), Path(args.output_root)
    receipts = [json.loads(_receipt_path(work, g).read_text(encoding="utf-8")) for g in chain]
    media = [
        _segment_media(decoder, _artifact(output_root, r), g.shown_frames)
        for g, r in zip(chain, receipts, strict=True)
    ]
    film_luma = [f for m in media for f in m["luma"]]
    film_audio = array("h")
    for m in media:
        film_audio.extend(m["audio"])
    diffs = so.frame_differences(film_luma)
    seams = []
    boundary = 0
    for index in range(1, len(chain)):
        boundary += chain[index - 1].shown_frames
        before, after = film_luma[boundary - 12 : boundary], film_luma[boundary : boundary + 12]
        seam_sample = boundary * METRIC_RATE // FPS
        successor = media[index]
        pred_frames, pred_audio, _ = load_compact(work, chain[index - 1].key)
        succ_frames, succ_audio, _ = load_compact(work, chain[index].key)
        generated = chain[index - 1].generated_frames
        pin_source = pred_frames[generated - PIN : generated]
        pin_audio = pred_audio[
            (generated - PIN) * METRIC_RATE // FPS : generated * METRIC_RATE // FPS
        ]
        head_audio = succ_audio[: PIN * METRIC_RATE // FPS]
        fidelity = so.pin_fidelity(succ_frames, pin_source)
        continuation = so.audio_continuation(head_audio, pin_audio, METRIC_RATE)
        seams.append(
            {
                "seam": index,
                "frame": boundary,
                "S1": _round(so.jump_ratio(before, after)),
                "S2": _round(so.luma_step(before, after)),
                "S3": _round(so.head_sharpness(successor["luma"], 96, successor["height"])),
                "S5": _round(so.band_energy_ratio(film_audio, METRIC_RATE, seam_sample)),
                "S6": _round(so.click_ratio(film_audio, METRIC_RATE, seam_sample)),
                "S8": {
                    "median_max_correlation": _round(continuation.median_max_correlation),
                    "median_lag_ms": _round(continuation.median_lag_ms),
                    "windows": continuation.windows,
                },
                "S9": _round(so.floor_level_step(film_audio, METRIC_RATE, seam_sample)),
                "S10_ms": _round(so.freeze_run_ms(diffs, boundary - 1, FPS)),
                "S12": {
                    "shown_mean": _round(fidelity.shown_mean),
                    "curve": [_round(v) for v in fidelity.curve],
                },
            }
        )
    bodies = [so.appearance_stats(m["rgb"][12:-12]) for m in media]
    drift = so.appearance_drift(bodies)
    fractions = [
        so.high_band_fraction(m["audio"][METRIC_RATE // 2 : -METRIC_RATE // 2], METRIC_RATE)
        for m in media
    ]
    exact = [
        exactness(tools, _artifact(output_root, r), frames=g.shown_frames)
        for g, r in zip(chain, receipts, strict=True)
    ]
    return {
        "prompt": chain[2].prompt,
        "seed": chain[1].seed,
        "arm": chain[1].arm,
        "seams": seams,
        "S4_drift": [_round(v) for v in drift],
        "S7": exact,
        "S11_dulling": [_round(v) for v in so.chain_audio_dulling(fractions)],
        "peak_vram_mib": max(r["peak_vram_mib"] for r in receipts[1:]),
        "wall_seconds": [r["wall_seconds"] for r in receipts],
    }


def double_decode_floor(work: Path, item: Generation) -> dict[str, float]:
    """S1 and S12 floors from two decodes of one latent (the host decode is nondeterministic)."""

    first, _, _ = load_compact(work, item.key)
    second, _, _ = load_compact(work, item.key + "-dd")
    splice = len(first) // 2
    generated = item.generated_frames
    s12 = so.pin_fidelity(second[generated - PIN :], first[generated - PIN : generated])
    return {
        "S1_splice": _round(
            so.jump_ratio(first[splice - 12 : splice], second[splice : splice + 12])
        ),
        "S12_shown_mean": _round(s12.shown_mean),
    }


def measure(args: argparse.Namespace) -> dict[str, Any]:
    work = Path(args.work)
    tools = MediaTools(Path(args.ffmpeg), Path(args.ffprobe))
    decoder = SeamObservationDecoder(Path(args.ffmpeg))
    chains = []
    floors = {}
    receipts = sorted((work / "receipts").glob("*.json"))
    keys = {path.stem for path in receipts}
    for prompt in PROMPTS:
        for seed in SEEDS:
            base = Generation(prompt, seed, "base", 1)
            if base.key in keys:
                floors[base.key] = double_decode_floor(work, base)
            for arm in ARMS:
                chain = _chain(prompt, seed, arm)
                if all(g.key in keys for g in chain):
                    started = time.monotonic()
                    result = measure_chain(args, decoder, tools, chain)
                    result["measure_seconds"] = round(time.monotonic() - started, 2)
                    chains.append(result)
                    print(
                        json.dumps(
                            {"measured": f"{prompt}-{seed}-{arm}", "s": result["measure_seconds"]}
                        ),
                        flush=True,
                    )
    return {"schema": SCHEMA, "double_decode_floor": floors, "chains": chains}


# --- contact sheets --------------------------------------------------------------------------


def sheets(args: argparse.Namespace) -> int:
    from PIL import Image

    work, output_root, target = Path(args.work), Path(args.output_root), Path(args.sheets)
    tools = MediaTools(Path(args.ffmpeg), Path(args.ffprobe))
    target.mkdir(parents=True, exist_ok=True)
    width, height = 216, scaled_height(WIDTH, HEIGHT, 216)
    count = 0
    keys = {path.stem for path in (work / "receipts").glob("*.json")}
    for prompt in PROMPTS:
        for seed in SEEDS:
            for arm in ARMS:
                chain = _chain(prompt, seed, arm)
                if not all(g.key in keys for g in chain):
                    continue
                for index in (1, 2):
                    frames = []
                    for g, first, last in (
                        (
                            chain[index - 1],
                            chain[index - 1].shown_frames - 6,
                            chain[index - 1].shown_frames,
                        ),
                        (chain[index], 0, 6),
                    ):
                        receipt = json.loads(_receipt_path(work, g).read_text(encoding="utf-8"))
                        raw = tools.run(
                            [
                                str(tools.ffmpeg),
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-nostdin",
                                "-i",
                                str(_artifact(output_root, receipt)),
                                "-map",
                                "0:v:0",
                                "-vf",
                                f"trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS,"
                                f"scale={width}:{height}:flags=area",
                                "-fps_mode",
                                "passthrough",
                                "-f",
                                "rawvideo",
                                "-pix_fmt",
                                "rgb24",
                                "pipe:1",
                            ],
                            limit=width * height * 3 * 6,
                        )
                        size = width * height * 3
                        frames += [raw[i * size : (i + 1) * size] for i in range(6)]
                    sheet = Image.new("RGB", (width * 6 + 5 * 2, height * 2 + 6), (255, 255, 255))
                    for position, frame in enumerate(frames):
                        tile = Image.frombytes("RGB", (width, height), frame)
                        sheet.paste(
                            tile, ((position % 6) * (width + 2), (position // 6) * (height + 6))
                        )
                    sheet.save(target / f"{prompt}-{seed}-{arm}-seam{index}.png", optimize=True)
                    count += 1
    return count


# --- entry -----------------------------------------------------------------------------------


def _items(args: argparse.Namespace) -> list[Generation]:
    if args.set == "base":
        return base_corpus()
    if args.set == "taper":
        return [
            g
            for g in corpus(
                {"motion": ("rl-taper", "rc-taper"), "dialogue": ("rl-taper", "rc-taper")}
            )
            if g.segment > 1
        ]
    if args.set == "rk12-dialogue":
        return [g for g in corpus({"dialogue": ("rk12",)}) if g.segment > 1]
    if args.set == "s4-neutral":
        arms = (*BASE_ARMS, *MOTION_ONLY_ARMS)
        return [Generation("motionb", seed, arm, 3) for seed in SEEDS for arm in arms]
    if args.set == "probe":
        return [Generation("motion", 1, "base", 1)]
    raise StudyError("unknown generation set")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "measure", "sheets", "graph"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8188")
    parser.add_argument("--canary-root", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--prefix", default="continuation-study")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument(
        "--set", default="base", choices=("base", "taper", "rk12-dialogue", "s4-neutral", "probe")
    )
    parser.add_argument("--result")
    parser.add_argument("--sheets")
    parser.add_argument("--key", help="graph: print one generation's graph")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            print(json.dumps({"generated": generate(args, _items(args))}))
        elif args.command == "measure":
            result = measure(args)
            Path(args.result).write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps({"chains": len(result["chains"])}))
        elif args.command == "sheets":
            print(json.dumps({"sheets": sheets(args)}))
        else:
            item = next(g for g in base_corpus() + _items(args) if g.key == args.key)
            print(json.dumps(build_graph(item, prefix=args.prefix), indent=2))
    except (StudyError, ProbeError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
