"""M19-08 weight-backed requalification runs against the explicitly supplied host.

One row per invocation, never more: every weight-backed run needs the maintainer's fresh
pre-run ceiling confirmation, so the executor refuses to batch rows behind one confirmation.

Usage (row 1, the joint latent descriptor):

    python scripts/m19_08_latent_row_requalification.py \
        --row joint_av_latent_descriptor \
        --host http://127.0.0.1:8188/ \
        --host-output-root <supplied host root>/output \
        --video-model "H3\\minimax_h3_fl2va_bf16.safetensors" \
        --text-encoder qwen3vl_32b_minimax_h3_int8_convrot.safetensors \
        --video-vae minimax_h3_video_vae_fp16.safetensors \
        --audio-vae minimax_h3_audio_vae_fp32.safetensors \
        --evidence .planning/260820-M19-08_LIVE_EVIDENCE.json \
        --ceiling-confirmed

`--ceiling-confirmed` asserts the maintainer confirmed the VRAM/disk/time ceiling immediately
before this invocation; without it the executor refuses to queue anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.m19_07.live import flac_streaminfo  # noqa: E402
from scripts.m19_08.runs import (  # noqa: E402
    VRAM_STANDARD_BYTES,
    ProbeError,
    ProbeExecutionError,
    VramSnapshot,
    WeightDesignation,
    assert_queue_idle,
    build_t2va_probe,
    free_host,
    queue_prompt,
    safetensors_header,
    vram_snapshot,
    wait_for_history,
)

#: The same probe geometry M19-07 used, and the same three canary lengths, so the weight-backed
#: descriptor observation lands on extents the weight-free decodes already confirmed.
PROBE_WIDTH = 384
PROBE_HEIGHT = 256
PROBE_STEPS = 4
PROBE_SEED = 1
DESCRIPTOR_LENGTHS = (39, 22, 124)
PROBE_PROMPT = "a plain gray studio wall"
RUN_TIMEOUT_SECONDS = 1800.0
IDLE_SETTLE_SECONDS = 15.0

EXIT_BLOCKED = 3


def _collect_outputs(output_root: Path, prefix: str) -> tuple[int, Path | None, Path | None]:
    """Count frames and locate the audio and latent files under one probe prefix."""

    probe_dir = output_root / prefix
    if not probe_dir.is_dir():
        raise ProbeError("the probe wrote no output directory")
    frames = 0
    audio_path: Path | None = None
    latent_path: Path | None = None
    for item in sorted(probe_dir.iterdir()):
        if not item.is_file():
            continue
        if item.suffix.lower() == ".png":
            frames += 1
        elif item.suffix.lower() == ".flac":
            audio_path = item
        elif item.suffix.lower() == ".latent":
            latent_path = item
    return frames, audio_path, latent_path


def _remove_outputs(output_root: Path, prefix: str) -> None:
    probe_dir = output_root / prefix
    if probe_dir.is_dir():
        shutil.rmtree(probe_dir)


def _serialization_probe(args: argparse.Namespace) -> dict[str, Any]:
    """Weight-free: does the host's own latent persistence accept the H3 joint AV latent?

    `EmptyMiniMaxH3LatentAV` -> `SaveLatent`. Success yields the safetensors header facts; an
    execution error yields the content-free failure facts. Either outcome is the measurement.
    No weight loads, so this sits outside the weight-backed ceiling protocol like the M19-07
    weight-free canaries did.
    """

    output_root = Path(args.host_output_root)
    prefix = "m19_08_desc_empty"
    prompt: dict[str, Any] = {
        "empty": {
            "class_type": "EmptyMiniMaxH3LatentAV",
            "inputs": {"width": PROBE_WIDTH, "height": PROBE_HEIGHT, "length": 39},
        },
        "save_l": {
            "class_type": "SaveLatent",
            "inputs": {"samples": ["empty", 0], "filename_prefix": f"{prefix}/latent"},
        },
    }
    assert_queue_idle(args.host)
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=300.0)
        _, _, latent_path = _collect_outputs(output_root, prefix)
        if latent_path is None:
            raise ProbeError("the serialization probe wrote no latent file")
        return {
            "outcome": "serialized",
            "latent_tensors": safetensors_header(latent_path),
        }
    except ProbeExecutionError as exc:
        return {"outcome": "native_serialization_failed", "facts": exc.facts}
    finally:
        _remove_outputs(output_root, prefix)


def _descriptor_row(
    args: argparse.Namespace,
    designation: WeightDesignation,
) -> dict[str, Any]:
    output_root = Path(args.host_output_root)
    if not output_root.is_dir():
        raise ProbeError("the supplied host output root does not exist")
    empty_latent = _serialization_probe(args)
    runs: list[dict[str, Any]] = []
    idle_baseline: VramSnapshot | None = None
    for length in DESCRIPTOR_LENGTHS:
        prefix = f"m19_08_desc_{length}"
        assert_queue_idle(args.host)
        before = vram_snapshot(args.host)
        if idle_baseline is None:
            idle_baseline = before
        if before.total_bytes > VRAM_STANDARD_BYTES:
            raise ProbeError("device total exceeds the 96 GB standard; refusing to run")
        prompt = build_t2va_probe(
            designation,
            prompt_text=PROBE_PROMPT,
            width=PROBE_WIDTH,
            height=PROBE_HEIGHT,
            length=length,
            steps=PROBE_STEPS,
            seed=PROBE_SEED,
            output_prefix=prefix,
            save_latent=False,
        )
        started = time.monotonic()
        try:
            prompt_id = queue_prompt(args.host, prompt)
            wait_for_history(args.host, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
            elapsed = round(time.monotonic() - started, 1)
            after = vram_snapshot(args.host)
            frames, audio_path, _ = _collect_outputs(output_root, prefix)
            if audio_path is None:
                raise ProbeError("the probe did not write an audio file")
            audio_facts = flac_streaminfo(audio_path.read_bytes())
        finally:
            # Free and clean on EVERY exit path: a failed run must not leave the shared
            # supplied host loaded or littered (the first launch of this row did).
            free_host(args.host)
            _remove_outputs(output_root, prefix)
        settled = vram_snapshot(args.host)
        runs.append(
            {
                "requested_length": length,
                "frames_decoded": frames,
                "audio": audio_facts.as_evidence(),
                "elapsed_seconds": elapsed,
                "vram": {
                    "before": before.as_evidence(),
                    "after_run": after.as_evidence(),
                    "after_free": settled.as_evidence(),
                },
                "within_96gb_standard": after.total_bytes <= VRAM_STANDARD_BYTES,
            }
        )
        time.sleep(IDLE_SETTLE_SECONDS)
    if idle_baseline is None:
        raise ProbeError("no run was executed")
    return {
        "row": "joint_av_latent_descriptor",
        "probe": {
            "topology": "t2va SamplerCustomAdvanced guidance-free (official t2v template)",
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "steps": PROBE_STEPS,
            "seed": PROBE_SEED,
        },
        "designation": designation.as_evidence(),
        "idle_baseline": idle_baseline.as_evidence(),
        "empty_latent_serialization": empty_latent,
        "runs": runs,
    }


RESUME_LENGTH = 22
RESUME_SPLIT_STEP = 2


def _output_digests(output_root: Path, prefix: str) -> dict[str, Any]:
    """Content-free identity of one probe's outputs: per-file sha256 folded in name order."""

    probe_dir = output_root / prefix
    if not probe_dir.is_dir():
        raise ProbeError("the probe wrote no output directory")
    video = hashlib.sha256()
    audio = hashlib.sha256()
    frames = 0
    for item in sorted(probe_dir.iterdir()):
        if not item.is_file():
            continue
        if item.suffix.lower() == ".png":
            video.update(hashlib.sha256(item.read_bytes()).digest())
            frames += 1
        elif item.suffix.lower() == ".flac":
            audio.update(item.read_bytes())
    return {
        "frames": frames,
        "video_digest": "sha256:" + video.hexdigest(),
        "audio_digest": "sha256:" + audio.hexdigest(),
    }


def _resume_probe_run(
    args: argparse.Namespace,
    designation: WeightDesignation,
    *,
    name: str,
    split_step: int | None,
    resume_from_denoised: bool = False,
) -> dict[str, Any]:
    output_root = Path(args.host_output_root)
    prefix = f"m19_08_resume_{name}"
    assert_queue_idle(args.host)
    before = vram_snapshot(args.host)
    if before.total_bytes > VRAM_STANDARD_BYTES:
        raise ProbeError("device total exceeds the 96 GB standard; refusing to run")
    prompt = build_t2va_probe(
        designation,
        prompt_text=PROBE_PROMPT,
        width=PROBE_WIDTH,
        height=PROBE_HEIGHT,
        length=RESUME_LENGTH,
        steps=PROBE_STEPS,
        seed=PROBE_SEED,
        output_prefix=prefix,
        save_latent=False,
        split_step=split_step,
    )
    if resume_from_denoised:
        # Continue stage 2 from the denoised projection instead of the noisy trajectory
        # sample: discriminates "wrong output wired" from a real resume divergence.
        prompt["stage2"]["inputs"]["latent_image"] = ["stage1", 1]
    started = time.monotonic()
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
        elapsed = round(time.monotonic() - started, 1)
        after = vram_snapshot(args.host)
        digests = _output_digests(output_root, prefix)
    finally:
        free_host(args.host)
        _remove_outputs(output_root, prefix)
    return {
        "name": name,
        "split_step": split_step,
        "resume_from_denoised": resume_from_denoised,
        "elapsed_seconds": elapsed,
        "vram": {"before": before.as_evidence(), "after_run": after.as_evidence()},
        "within_96gb_standard": after.total_bytes <= VRAM_STANDARD_BYTES,
        **digests,
    }


def _resume_row(
    args: argparse.Namespace,
    designation: WeightDesignation,
) -> dict[str, Any]:
    """Row 2: completed_boundary_resume. M19-07 saw the two-stage resume diverge from the
    single pass and could not explain it. The missing control is host determinism itself:
    an identical single-pass rerun. Only if the host reproduces its own run bit-exactly can
    a resume divergence be attributed to the resume composition."""

    output_root = Path(args.host_output_root)
    if not output_root.is_dir():
        raise ProbeError("the supplied host output root does not exist")
    baseline = _resume_probe_run(args, designation, name="single_a", split_step=None)
    repeat = _resume_probe_run(args, designation, name="single_b", split_step=None)
    resumed = _resume_probe_run(
        args, designation, name="split_resume", split_step=RESUME_SPLIT_STEP
    )
    runs = [baseline, repeat, resumed]
    host_deterministic = (
        baseline["video_digest"] == repeat["video_digest"]
        and baseline["audio_digest"] == repeat["audio_digest"]
    )
    resume_matches = (
        baseline["video_digest"] == resumed["video_digest"]
        and baseline["audio_digest"] == resumed["audio_digest"]
    )
    denoised_matches: bool | None = None
    if host_deterministic and not resume_matches:
        denoised = _resume_probe_run(
            args,
            designation,
            name="split_resume_denoised",
            split_step=RESUME_SPLIT_STEP,
            resume_from_denoised=True,
        )
        runs.append(denoised)
        denoised_matches = (
            baseline["video_digest"] == denoised["video_digest"]
            and baseline["audio_digest"] == denoised["audio_digest"]
        )
    return {
        "row": "completed_boundary_resume",
        "probe": {
            "topology": "t2va SamplerCustomAdvanced guidance-free (official t2v template)",
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "length": RESUME_LENGTH,
            "steps": PROBE_STEPS,
            "seed": PROBE_SEED,
            "split_step": RESUME_SPLIT_STEP,
        },
        "designation": designation.as_evidence(),
        "host_deterministic_across_identical_runs": host_deterministic,
        "resume_matches_single_pass": resume_matches,
        "denoised_resume_matches_single_pass": denoised_matches,
        "runs": runs,
    }


MASK_LENGTH = 22


def _empty_decode_run(args: argparse.Namespace) -> dict[str, Any]:
    """Weight-free baseline: decode the empty joint AV latent (the M19-07 canary shape)."""

    output_root = Path(args.host_output_root)
    prefix = "m19_08_mask_empty_decode"
    prompt: dict[str, Any] = {
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": args.video_vae}},
        "av": {"class_type": "VAELoader", "inputs": {"vae_name": args.audio_vae}},
        "empty": {
            "class_type": "EmptyMiniMaxH3LatentAV",
            "inputs": {"width": PROBE_WIDTH, "height": PROBE_HEIGHT, "length": MASK_LENGTH},
        },
        "decode_v": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["empty", 0], "vae": ["vv", 0]},
        },
        "decode_a": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["empty", 0], "vae": ["av", 0]},
        },
        "save_v": {
            "class_type": "SaveImage",
            "inputs": {"images": ["decode_v", 0], "filename_prefix": f"{prefix}/frame"},
        },
        "save_a": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["decode_a", 0], "filename_prefix": f"{prefix}/audio"},
        },
    }
    assert_queue_idle(args.host)
    started = time.monotonic()
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=600.0)
        digests = _output_digests(Path(args.host_output_root), prefix)
    finally:
        free_host(args.host)
        _remove_outputs(output_root, prefix)
    return {
        "name": "empty_decode",
        "weight_free": True,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        **digests,
    }


def _mask_probe_run(
    args: argparse.Namespace,
    designation: WeightDesignation,
    *,
    name: str,
    mask_value: float | None,
) -> dict[str, Any]:
    output_root = Path(args.host_output_root)
    prefix = f"m19_08_mask_{name}"
    assert_queue_idle(args.host)
    before = vram_snapshot(args.host)
    if before.total_bytes > VRAM_STANDARD_BYTES:
        raise ProbeError("device total exceeds the 96 GB standard; refusing to run")
    prompt = build_t2va_probe(
        designation,
        prompt_text=PROBE_PROMPT,
        width=PROBE_WIDTH,
        height=PROBE_HEIGHT,
        length=MASK_LENGTH,
        steps=PROBE_STEPS,
        seed=PROBE_SEED,
        output_prefix=prefix,
        save_latent=False,
        solid_mask_value=mask_value,
    )
    started = time.monotonic()
    outcome: dict[str, Any]
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
        after = vram_snapshot(args.host)
        outcome = {
            "outcome": "executed",
            "vram": {"before": before.as_evidence(), "after_run": after.as_evidence()},
            "within_96gb_standard": after.total_bytes <= VRAM_STANDARD_BYTES,
            **_output_digests(output_root, prefix),
        }
    except ProbeExecutionError as exc:
        outcome = {"outcome": "execution_failed", "facts": exc.facts}
    finally:
        free_host(args.host)
        _remove_outputs(output_root, prefix)
    outcome["name"] = name
    outcome["mask_value"] = mask_value
    outcome["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return outcome


def _mask_row(
    args: argparse.Namespace,
    designation: WeightDesignation,
) -> dict[str, Any]:
    """Row 3: dual_domain_av_mask. The bounded search: the native latent-mask mechanism is
    `SolidMask` -> `SetLatentNoiseMask` (comfy core); community packs are outside the claim.
    Discriminators: an all-open mask (1.0) must reproduce the unmasked run; an all-protect
    mask (0.0) must freeze the video domain at the empty latent's decode; whether the audio
    digest moves under a 0.0 mask states whether the mask reaches the audio domain at all."""

    output_root = Path(args.host_output_root)
    if not output_root.is_dir():
        raise ProbeError("the supplied host output root does not exist")
    empty_decode = _empty_decode_run(args)
    unmasked = _mask_probe_run(args, designation, name="unmasked", mask_value=None)
    open_mask = _mask_probe_run(args, designation, name="open_1", mask_value=1.0)
    protect_mask = _mask_probe_run(args, designation, name="protect_0", mask_value=0.0)
    runs = [empty_decode, unmasked, open_mask, protect_mask]

    def _digest_pair(run: dict[str, Any]) -> tuple[Any, Any]:
        return run.get("video_digest"), run.get("audio_digest")

    executed = all(
        item.get("outcome") == "executed" for item in (unmasked, open_mask, protect_mask)
    )
    facts: dict[str, Any] = {"all_probes_executed": executed}
    if executed:
        facts["open_mask_reproduces_unmasked"] = _digest_pair(open_mask) == _digest_pair(unmasked)
        facts["protect_mask_video_frozen_at_empty_decode"] = protect_mask.get(
            "video_digest"
        ) == empty_decode.get("video_digest")
        facts["protect_mask_changes_video_vs_unmasked"] = protect_mask.get(
            "video_digest"
        ) != unmasked.get("video_digest")
        facts["protect_mask_audio_equals_unmasked"] = protect_mask.get(
            "audio_digest"
        ) == unmasked.get("audio_digest")
        facts["protect_mask_audio_frozen_at_empty_decode"] = protect_mask.get(
            "audio_digest"
        ) == empty_decode.get("audio_digest")
    return {
        "row": "dual_domain_av_mask",
        "probe": {
            "topology": "t2va SamplerCustomAdvanced guidance-free (official t2v template)",
            "bounded_search": "native mechanism only: SolidMask -> SetLatentNoiseMask",
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "length": MASK_LENGTH,
            "steps": PROBE_STEPS,
            "seed": PROBE_SEED,
        },
        "designation": designation.as_evidence(),
        "findings": facts,
        "runs": runs,
    }


BRIDGE_LENGTH = 22


def _latent_combinator_probe(
    args: argparse.Namespace, *, name: str, node: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    """Weight-free: does a stock dense-latent combinator accept two H3 NestedTensors?"""

    output_root = Path(args.host_output_root)
    prefix = f"m19_08_bridge_{name}"
    prompt: dict[str, Any] = {
        "av": {"class_type": "VAELoader", "inputs": {"vae_name": args.audio_vae}},
        "e1": {
            "class_type": "EmptyMiniMaxH3LatentAV",
            "inputs": {"width": PROBE_WIDTH, "height": PROBE_HEIGHT, "length": BRIDGE_LENGTH},
        },
        "e2": {
            "class_type": "EmptyMiniMaxH3LatentAV",
            "inputs": {"width": PROBE_WIDTH, "height": PROBE_HEIGHT, "length": BRIDGE_LENGTH},
        },
        "combine": {
            "class_type": node,
            "inputs": {"samples1": ["e1", 0], "samples2": ["e2", 0], **inputs},
        },
        "da": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["combine", 0], "vae": ["av", 0]},
        },
        "sa": {
            "class_type": "SaveAudio",
            "inputs": {"audio": ["da", 0], "filename_prefix": f"{prefix}/audio"},
        },
    }
    assert_queue_idle(args.host)
    started = time.monotonic()
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=600.0)
        outcome: dict[str, Any] = {"outcome": "executed"}
    except ProbeExecutionError as exc:
        outcome = {"outcome": "execution_failed", "facts": exc.facts}
    finally:
        free_host(args.host)
        _remove_outputs(output_root, prefix)
    outcome["name"] = name
    outcome["node"] = node
    outcome["weight_free"] = True
    outcome["elapsed_seconds"] = round(time.monotonic() - started, 1)
    return outcome


def _bridge_row(
    args: argparse.Namespace,
    designation: WeightDesignation,
) -> dict[str, Any]:
    """Row 4: two_ended_av_bridge. The official two-ended composition is fl2va
    (`MiniMaxH3ImageToVideo` with both keyframes); the probe drives it end to end with two
    synthetic solid frames. Latent-domain combination is probed weight-free with the stock
    dense combinators. The audio side is settled by the pinned source: keyframe anchors are
    video-only and reference audio is advisory, so no native audio two-ended anchor exists."""

    output_root = Path(args.host_output_root)
    if not output_root.is_dir():
        raise ProbeError("the supplied host output root does not exist")
    interp = _latent_combinator_probe(
        args, name="interpolate", node="LatentInterpolate", inputs={"ratio": 0.5}
    )
    concat = _latent_combinator_probe(
        args, name="concat_time", node="LatentConcat", inputs={"dim": "t"}
    )

    prefix = "m19_08_bridge_fl2va"
    assert_queue_idle(args.host)
    before = vram_snapshot(args.host)
    if before.total_bytes > VRAM_STANDARD_BYTES:
        raise ProbeError("device total exceeds the 96 GB standard; refusing to run")
    prompt = build_t2va_probe(
        designation,
        prompt_text=PROBE_PROMPT,
        width=PROBE_WIDTH,
        height=PROBE_HEIGHT,
        length=BRIDGE_LENGTH,
        steps=PROBE_STEPS,
        seed=PROBE_SEED,
        output_prefix=prefix,
        save_latent=False,
    )
    prompt["frame_a"] = {
        "class_type": "EmptyImage",
        "inputs": {
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "batch_size": 1,
            "color": 0,
        },
    }
    prompt["frame_b"] = {
        "class_type": "EmptyImage",
        "inputs": {
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "batch_size": 1,
            "color": 16777215,
        },
    }
    prompt["h3"]["inputs"]["first_frame"] = ["frame_a", 0]
    prompt["h3"]["inputs"]["last_frame"] = ["frame_b", 0]
    started = time.monotonic()
    fl2va: dict[str, Any]
    try:
        prompt_id = queue_prompt(args.host, prompt)
        wait_for_history(args.host, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
        after = vram_snapshot(args.host)
        digests = _output_digests(output_root, prefix)
        fl2va = {
            "outcome": "executed",
            "vram": {"before": before.as_evidence(), "after_run": after.as_evidence()},
            "within_96gb_standard": after.total_bytes <= VRAM_STANDARD_BYTES,
            **digests,
        }
    except ProbeExecutionError as exc:
        fl2va = {"outcome": "execution_failed", "facts": exc.facts}
    finally:
        free_host(args.host)
        _remove_outputs(output_root, prefix)
    fl2va["name"] = "fl2va_two_ended"
    fl2va["elapsed_seconds"] = round(time.monotonic() - started, 1)

    return {
        "row": "two_ended_av_bridge",
        "probe": {
            "topology": "fl2va two-keyframe conditioning + weight-free dense combinators",
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "length": BRIDGE_LENGTH,
            "steps": PROBE_STEPS,
            "seed": PROBE_SEED,
        },
        "designation": designation.as_evidence(),
        "source_facts": {
            "keyframe_anchors": (
                "MiniMaxH3ImageToVideo pins first_frame at resolved_frame_index 0 and "
                "last_frame at frame_count-1; keyframe condition latents are re-injected "
                "every sampling step and never denoised (pinned nodes_minimax_h3.py)"
            ),
            "keyframe_domain": (
                "video only: keyframes are vae.encode(image); no audio anchor input exists"
            ),
            "audio_reference": (
                "MiniMaxH3ReferenceToVideo accepts standalone/soundtrack reference audio as "
                "advisory conditioning (<Audio j>), not as an authoritative anchor"
            ),
        },
        "runs": [interp, concat, fl2va],
    }


ROW_EXECUTORS = {
    "joint_av_latent_descriptor": _descriptor_row,
    "completed_boundary_resume": _resume_row,
    "dual_domain_av_mask": _mask_row,
    "two_ended_av_bridge": _bridge_row,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", required=True, choices=sorted(ROW_EXECUTORS))
    parser.add_argument("--host", required=True)
    parser.add_argument("--host-output-root", required=True)
    parser.add_argument("--video-model", required=True)
    parser.add_argument("--text-encoder", required=True)
    parser.add_argument("--video-vae", required=True)
    parser.add_argument("--audio-vae", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--ceiling-confirmed",
        action="store_true",
        help="assert the maintainer confirmed the pre-run ceiling immediately before this run",
    )
    args = parser.parse_args()
    if not args.ceiling_confirmed:
        print("refused: the maintainer's immediate pre-run ceiling confirmation is required")
        return EXIT_BLOCKED
    designation = WeightDesignation(
        video_model=args.video_model,
        text_encoder=args.text_encoder,
        video_vae=args.video_vae,
        audio_vae=args.audio_vae,
    )
    try:
        result = ROW_EXECUTORS[args.row](args, designation)
    except ProbeError as exc:
        print(f"BLOCKED: {exc}")
        return EXIT_BLOCKED
    evidence_path = Path(args.evidence)
    existing: dict[str, Any] = {}
    if evidence_path.is_file():
        existing = json.loads(evidence_path.read_text(encoding="utf-8"))
    existing.setdefault("schema", "h3-context-m19-08-live-evidence/1")
    existing.setdefault("rows", {})
    existing["rows"][args.row] = result
    evidence_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"row": args.row, "runs": len(result.get("runs", [])), "status": "OK"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
