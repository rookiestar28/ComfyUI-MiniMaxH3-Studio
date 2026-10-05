"""Owned isolated worker entry point. Never installs packages or downloads weights."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import sys
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

MODEL = "qwen3.8:27b-q4_K_M"
OLLAMA_VERSION = "0.35.1"
MAX_VISUAL_DESCRIPTIONS = 5
# IMPORTANT: featureless frames still carry visible color evidence. Omitting this case
# makes the model return empty descriptions for valid solid-color reference windows.
VISUAL_PROMPT = (
    "Describe only visible objects, colors and shapes in this single frame. "
    "A uniform-colored frame is valid visual evidence: describe its visible color "
    "even when no object is present. Do not infer time, motion, speech or unseen details. "
    "If the image cannot be interpreted, use an empty description. Return JSON with description."
)
MODEL_SHA = "25b843619e944cd0ae6069f94ff4e5e26a16e109ccbc0a66a0f05979ed70098e"
WEIGHT_SHA = "a8e94b85976e5864ba3e9525c7e6c83b2a1eca42d4b797a0c7c24d778e40fd95"
ASSET_HASHES = {
    "config.json": "ad0e8d1e46f4d01f7861a21509e5d0f977d6cc1f367a370603c92541d819807b",
    "generation_config.json": "fbdfa70135de9b1d31553393f14e80aaeb1936ea36576b2ba864055943c09d23",
    "preprocessor_config.json": "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
    "tokenizer.json": "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
    "tokenizer_config.json": "844b642c73a91359722f47b35705f7174686df33d252695d8572cf9ac03a6389",
    "special_tokens_map.json": "1c70773c078cb2ca96e0fcff113102f1d3e2b1504272c3bb63b035d4a6700d87",
    "vocab.json": "e2aa043ef015641d363d8288e7c241c85e36a5c761fb303598e0710233344387",
    "merges.txt": "2df2990a395e35e8dfbc7511e08c12d56018d8d04691e0133e5d63b21e154dc6",
    "normalizer.json": "bf1c507dc8724ca9cf9903640dacfb69dae2f00edee4f21ceba106a7392f26dd",
    "added_tokens.json": "3c51f66c4c21f9e126970078f11ae77a78c74aee8df606ee9daba86e467108e0",
}


class Refusal(Exception):
    """Finite content-free worker failure."""


def _http(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    # SECURITY: fixed loopback, no proxy environment, redirects or arbitrary URLs.
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=45)
    try:
        raw = None if body is None else json.dumps(body).encode()
        connection.request(
            "GET" if raw is None else "POST", path, raw, {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        content = response.read(65537)
        if response.status != 200 or len(content) > 65536:
            raise Refusal("invalid_runtime_response")
        value = json.loads(content)
        if type(value) is not dict:
            raise Refusal("invalid_runtime_response")
        return value
    finally:
        connection.close()


def _ollama_identity() -> None:
    if _http("/api/version").get("version") != OLLAMA_VERSION:
        raise Refusal("runtime_drift")
    rows = _http("/api/tags").get("models")
    if type(rows) is not list or not any(
        type(row) is dict and row.get("name") == MODEL and row.get("digest") == MODEL_SHA
        for row in rows
    ):
        raise Refusal("runtime_drift")


def _visual(job: dict[str, object]) -> list[str]:
    _ollama_identity()
    running = _http("/api/ps").get("models")
    if type(running) is not list or any(
        type(row) is not dict or row.get("name") != MODEL for row in running
    ):
        raise Refusal("runtime_busy")
    frames = job["frames"]
    # IMPORTANT: the isolated worker and parent import this same bound and attest it in the job.
    # A one-sided increase otherwise admits frames that the other side refuses or truncates.
    if (
        type(job.get("max_visual_descriptions")) is not int
        or job["max_visual_descriptions"] != MAX_VISUAL_DESCRIPTIONS
    ):
        raise Refusal("input_budget")
    if type(frames) is not list or not 1 <= len(frames) <= MAX_VISUAL_DESCRIPTIONS:
        raise Refusal("unsupported_media")
    descriptions = []
    for frame in frames:
        reply = _http(
            "/api/chat",
            {
                "model": MODEL,
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {"num_ctx": 4096, "num_predict": 128, "temperature": 0, "seed": 0},
                "format": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                    "required": ["description"],
                    "additionalProperties": False,
                },
                "messages": [
                    {
                        "role": "user",
                        "images": [frame],
                        "content": VISUAL_PROMPT,
                    }
                ],
            },
        )
        if reply.get("done") is not True or reply.get("done_reason") != "stop":
            raise Refusal("incomplete_output")
        message = reply.get("message")
        if type(message) is not dict or type(message.get("content")) is not str:
            raise Refusal("invalid_runtime_response")
        value = json.loads(message["content"])
        if type(value) is not dict or set(value) != {"description"}:
            raise Refusal("invalid_runtime_response")
        text = value["description"]
        if type(text) is not str or not text.strip() or len(text) > 512:
            raise Refusal("empty_or_unbounded_output")
        descriptions.append(text.strip())
    _ollama_identity()
    return descriptions


def _audio(job: dict[str, object]) -> list[str]:
    if version("transformers") != "4.57.6" or version("torch") != "2.14.0+cpu":
        raise Refusal("runtime_drift")
    np = import_module("numpy")
    torch = import_module("torch")
    load = import_module("safetensors.torch").load
    resample_poly = import_module("scipy.signal").resample_poly
    transformers = import_module("transformers")
    GenerationConfig = transformers.GenerationConfig
    WhisperConfig = transformers.WhisperConfig
    WhisperForConditionalGeneration = transformers.WhisperForConditionalGeneration
    WhisperProcessor = transformers.WhisperProcessor

    source = Path(str(job["processor_dir"]))
    assets = Path("assets")
    assets.mkdir()
    # CRITICAL: hash and consume the same bytes; reopening checked paths permits drift.
    for name, expected in ASSET_HASHES.items():
        with (source / name).open("rb") as stream:
            raw = stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
            raise Refusal("runtime_drift")
        (assets / name).write_bytes(raw)
    weight = Path(str(job["weight_path"]))
    if weight.stat().st_size != 3087130976:
        raise Refusal("runtime_drift")
    with weight.open("rb") as stream:
        raw = stream.read(3087130977)
    if len(raw) != 3087130976 or hashlib.sha256(raw).hexdigest() != WEIGHT_SHA:
        raise Refusal("runtime_drift")
    config = WhisperConfig.from_json_file(str(assets / "config.json"))
    with torch.device("meta"):
        model = WhisperForConditionalGeneration(config)
    state = load(raw)
    del raw
    missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
    if missing != ["proj_out.weight"] or unexpected:
        raise Refusal("runtime_drift")
    model.tie_weights()
    model.generation_config = GenerationConfig.from_dict(
        json.loads((assets / "generation_config.json").read_text())
    )
    del state
    torch.set_num_threads(8)
    model.eval().to("cpu", dtype=torch.float32)
    processor = WhisperProcessor.from_pretrained(
        str(assets), local_files_only=True, trust_remote_code=False
    )
    samples = np.frombuffer(base64.b64decode(str(job["samples"]), validate=True), dtype="<f4")
    rate = job["sample_rate"]
    if type(rate) is not int or not 8000 <= rate <= 48000 or not 0 < len(samples) <= rate * 15:
        raise Refusal("unsupported_media")
    if (
        not np.isfinite(samples).all()
        or np.abs(samples).max() > 1
        or np.abs(samples).max() < 0.0001
    ):
        raise Refusal("empty_audio")
    divisor = math.gcd(rate, 16000)
    samples = resample_poly(samples, 16000 // divisor, rate // divisor).astype(np.float32)
    inputs = processor(
        samples, sampling_rate=16000, return_tensors="pt", return_attention_mask=True
    )
    with torch.inference_mode():
        output = model.generate(
            inputs.input_features,
            attention_mask=inputs.attention_mask,
            language="en",
            task="transcribe",
            max_new_tokens=96,
            max_time=60,
            do_sample=False,
            num_beams=1,
            return_timestamps=False,
            return_dict_in_generate=True,
        )
    sequences = _completed_asr_sequences(output, model.generation_config.eos_token_id)
    text = processor.batch_decode(sequences, skip_special_tokens=True)[0].strip()
    if not text or len(text) > 4096:
        raise Refusal("empty_or_unbounded_output")
    return [text]


def _completed_asr_sequences(output: object, eos: int) -> object:
    # CRITICAL: Whisper's default short-form tensor strips EOS. Inspect the underlying
    # generation result instead; checking the stripped tensor rejects completed speech.
    sequences = getattr(output, "sequences", None)
    if (
        sequences is None
        or len(sequences.shape) != 2
        or sequences.shape[0] != 1
        or not 1 <= sequences.shape[1] <= 100  # four prompt tokens plus max96 new tokens
        or sequences[0, -1].item() != eos
    ):
        raise Refusal("incomplete_output")
    return sequences


def main() -> None:
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise Refusal("input_budget")
        job = json.loads(raw)
        if job["profile_id"] == "qwen38_27b_q4_visual_v2":
            descriptions = _visual(job)
        elif job["profile_id"] == "whisper_large_v3_cpu_en_v1":
            descriptions = _audio(job)
        else:
            raise Refusal("profile_unavailable")
        print(json.dumps({"ok": True, "descriptions": descriptions}), flush=True)
    except Refusal as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}), flush=True)
    except Exception:
        # Never export paths, provider payloads, prompts or dependency exception messages.
        print(json.dumps({"ok": False, "reason": "runtime_unavailable"}), flush=True)


if __name__ == "__main__":
    main()
