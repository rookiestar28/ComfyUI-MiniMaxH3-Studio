"""Weight-backed probe execution against the explicitly supplied host.

Every run here follows the M19-07 hygiene protocol: the queue must be idle before a probe is
queued, VRAM is snapshotted before and after, the runtime's cached weights are released after,
and the probe's outputs are removed from the host once observed. The 96 GB standard is judged
from the host's own `/system_stats`: a run whose device total is at or under the standard is in
budget by definition, and a run that fails for memory keeps its honest failure.

Evidence discipline: weight designations are recorded as the installed filenames the maintainer
named (host-relative identifiers, no private absolute paths); latent observation is header-only
(safetensors metadata: tensor names, dtypes, shapes) and no tensor, pixel or sample content is
ever read into evidence.
"""

from __future__ import annotations

import json
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The 96 GB resource standard, authorized 2026-08-20, expressed in bytes (binary GiB).
VRAM_STANDARD_BYTES = 96 * 1024**3

_JSON_HEADERS = {"Content-Type": "application/json"}


class ProbeError(RuntimeError):
    """A probe could not run or observe; the row keeps its honest outcome."""


class ProbeExecutionError(ProbeError):
    """The host executed the probe and reported an error; the facts are the measurement.

    Carries only content-free facts: node type, exception type, exception message. Prompts,
    paths and tensor content never enter it.
    """

    def __init__(self, facts: dict[str, str]) -> None:
        super().__init__(
            "probe execution failed: "
            + " ".join(f"{key}={value}" for key, value in sorted(facts.items()))
        )
        self.facts = facts


def _error_facts(status: dict[str, Any]) -> dict[str, str]:
    for message in status.get("messages") or []:
        if (
            isinstance(message, (list, tuple))
            and len(message) > 1
            and message[0] == "execution_error"
            and isinstance(message[1], dict)
        ):
            detail = message[1]
            return {
                "node_type": str(detail.get("node_type")),
                "exception_type": str(detail.get("exception_type")),
                "exception_message": str(detail.get("exception_message"))[:300],
            }
    return {"exception_type": "unknown"}


@dataclass(frozen=True)
class WeightDesignation:
    """The maintainer-named installed official weights, one per model role."""

    video_model: str
    text_encoder: str
    video_vae: str
    audio_vae: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "video_model": self.video_model,
            "text_encoder": self.text_encoder,
            "video_vae": self.video_vae,
            "audio_vae": self.audio_vae,
        }


@dataclass(frozen=True)
class VramSnapshot:
    total_bytes: int
    free_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.free_bytes

    def as_evidence(self) -> dict[str, int]:
        return {
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "used_bytes": self.used_bytes,
        }


def _request(base_url: str, route: str, *, payload: Any = None, timeout: float = 60.0) -> Any:
    url = base_url.rstrip("/") + route
    if not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        raise ProbeError("the supplied host is the only permitted endpoint")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    # S310: the scheme is constrained above -- only the supplied loopback host is reachable.
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=_JSON_HEADERS if data else {}
    )
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(5)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ProbeError(f"host refused {route}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # The shared host can be briefly unresponsive right after a /free; retry twice.
            last_error = exc
    else:
        raise ProbeError(f"host request failed: {route}") from last_error
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def vram_snapshot(base_url: str) -> VramSnapshot:
    stats = _request(base_url, "/system_stats")
    devices = stats.get("devices") if isinstance(stats, dict) else None
    if not devices:
        raise ProbeError("the host reported no device")
    device = devices[0]
    return VramSnapshot(
        total_bytes=int(device["vram_total"]),
        free_bytes=int(device["vram_free"]),
    )


def assert_queue_idle(base_url: str) -> None:
    queue = _request(base_url, "/queue")
    if not isinstance(queue, dict):
        raise ProbeError("the host queue could not be read")
    if queue.get("queue_running") or queue.get("queue_pending"):
        raise ProbeError("the host queue is not idle; refusing to add probe load")


def free_host(base_url: str) -> None:
    _request(base_url, "/free", payload={"unload_models": True, "free_memory": True})


def queue_prompt(base_url: str, prompt: dict[str, Any]) -> str:
    payload = {"prompt": prompt, "client_id": f"m19-08-{uuid.uuid4().hex[:12]}"}
    result = _request(base_url, "/prompt", payload=payload)
    if not isinstance(result, dict) or "prompt_id" not in result:
        raise ProbeError("the host did not accept the probe prompt")
    return str(result["prompt_id"])


def wait_for_history(
    base_url: str,
    prompt_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        history = _request(base_url, f"/history/{prompt_id}")
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise ProbeExecutionError(_error_facts(status))
            if entry.get("outputs"):
                return entry
        time.sleep(poll_seconds)
    raise ProbeError("probe did not complete within its bounded timeout")


def build_t2va_probe(
    designation: WeightDesignation,
    *,
    prompt_text: str,
    width: int,
    height: int,
    length: int,
    steps: int,
    seed: int,
    output_prefix: str,
    save_latent: bool,
    split_step: int | None = None,
    resume_with_disable_noise: bool = True,
    solid_mask_value: float | None = None,
) -> dict[str, Any]:
    """The guidance-free `SamplerCustomAdvanced` topology of the official t2v template.

    `split_step` builds the two-stage resume composition (`SplitSigmas` at that step, second
    stage continuing from the first stage's latent); `solid_mask_value` applies
    `SetLatentNoiseMask` over the joint latent before sampling. Both compose with the plain
    structural run so every probe stays one deterministic seeded topology.
    """

    prompt: dict[str, Any] = {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": designation.video_model, "weight_dtype": "default"},
        },
        "c": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": designation.text_encoder,
                "type": "minimax",
                "device": "default",
            },
        },
        "vv": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": designation.video_vae},
        },
        "av": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": designation.audio_vae},
        },
        "h3": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["c", 0],
                "vae": ["vv", 0],
                "prompt": prompt_text,
                "width": width,
                "height": height,
                "length": length,
            },
        },
        "guider": {
            "class_type": "BasicGuider",
            "inputs": {"model": ["u", 0], "conditioning": ["h3", 0]},
        },
        "sampler": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "res_multistep"},
        },
        "sigmas": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["u", 0], "scheduler": "simple", "steps": steps, "denoise": 1},
        },
        "noise": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
    }
    latent_source: list[Any] = ["h3", 1]
    if solid_mask_value is not None:
        prompt["mask"] = {
            "class_type": "SolidMask",
            "inputs": {"value": solid_mask_value, "width": width, "height": height},
        }
        prompt["masked"] = {
            "class_type": "SetLatentNoiseMask",
            "inputs": {"samples": latent_source, "mask": ["mask", 0]},
        }
        latent_source = ["masked", 0]
    if split_step is None:
        prompt["run"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise", 0],
                "guider": ["guider", 0],
                "sampler": ["sampler", 0],
                "sigmas": ["sigmas", 0],
                "latent_image": latent_source,
            },
        }
        final_latent: list[Any] = ["run", 0]
    else:
        prompt["split"] = {
            "class_type": "SplitSigmas",
            "inputs": {"sigmas": ["sigmas", 0], "step": split_step},
        }
        prompt["stage1"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise", 0],
                "guider": ["guider", 0],
                "sampler": ["sampler", 0],
                "sigmas": ["split", 0],
                "latent_image": latent_source,
            },
        }
        second_noise = "noise2"
        prompt[second_noise] = (
            {"class_type": "DisableNoise", "inputs": {}}
            if resume_with_disable_noise
            else {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        )
        prompt["stage2"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": [second_noise, 0],
                "guider": ["guider", 0],
                "sampler": ["sampler", 0],
                "sigmas": ["split", 1],
                "latent_image": ["stage1", 0],
            },
        }
        final_latent = ["stage2", 0]
    prompt["decode_v"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": final_latent, "vae": ["vv", 0]},
    }
    prompt["decode_a"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": final_latent, "vae": ["av", 0]},
    }
    prompt["save_v"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["decode_v", 0], "filename_prefix": f"{output_prefix}/frame"},
    }
    prompt["save_a"] = {
        "class_type": "SaveAudio",
        "inputs": {"audio": ["decode_a", 0], "filename_prefix": f"{output_prefix}/audio"},
    }
    if save_latent:
        prompt["save_l"] = {
            "class_type": "SaveLatent",
            "inputs": {"samples": final_latent, "filename_prefix": f"{output_prefix}/latent"},
        }
    return prompt


def safetensors_header(path: Path, *, maximum_header_bytes: int = 1 << 20) -> dict[str, Any]:
    """Read only the safetensors JSON header: tensor names, dtypes, shapes. Never the data."""

    with open(path, "rb") as handle:
        raw_length = handle.read(8)
        if len(raw_length) != 8:
            raise ProbeError("latent file is shorter than a safetensors length field")
        (header_length,) = struct.unpack("<Q", raw_length)
        if not 0 < header_length <= maximum_header_bytes:
            raise ProbeError("latent header length is out of bounds")
        header = handle.read(header_length)
    if len(header) != header_length:
        raise ProbeError("latent header is truncated")
    try:
        parsed = json.loads(header)
    except json.JSONDecodeError as exc:
        raise ProbeError("latent header is not JSON") from exc
    if not isinstance(parsed, dict):
        raise ProbeError("latent header is not an object")
    projected: dict[str, Any] = {}
    for name, spec in parsed.items():
        if name == "__metadata__":
            continue
        if isinstance(spec, dict):
            projected[name] = {
                "dtype": spec.get("dtype"),
                "shape": spec.get("shape"),
            }
    return projected
