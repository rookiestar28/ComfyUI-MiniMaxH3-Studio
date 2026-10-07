"""M20-05 exact latent checkpoint resume qualification against the supplied host.

One invocation runs the whole frozen sequence: workflow A executes stage-1 of the qualified
split schedule and persists its trajectory latent through the save canary (real codec, real
M20-04 store, declared boundary); the harness re-inspects the checkpoint through the real
store API and executes the REAL admission decision script-side; workflow B admits the same
checkpoint through the load canary IN the host process and finishes generation through the
pure `build_resume_composition` fragment; a negative workflow proves a settings-identity
divergence refuses with its classified reason code instead of sampling.

The resume oracle is STRUCTURAL, per the M19-08 row (`structural_resume_executes_no_bitexact
oracle`): workflow B contains no stage-1 sampler at all, its only sampler consumes the split
schedule's low tail (`total - completed` steps by sigma arithmetic), and the decoded extents
must be exact (frames == requested; audio samples on the 800-sample boundary rule).
Wall-clock is recorded as supporting evidence only. Bit-exact comparison is impossible on
this subject and appears nowhere.

Hygiene follows the M19-08 protocol: queue idle before every run, VRAM snapshots around it,
`/free` and host-output removal afterwards, and the harness-owned canary root is emptied at
the end unless `--keep-root` asks to retain it. Evidence stays content-free: fingerprints,
extents, node types and timings; never tensors, frames, audio, prompts beyond the repo's own
probe constant, or private absolute paths.

Authorization: the M19-08 weight designation and the 96 GB ceiling confirmation are STANDING
per the maintainer directive of 2026-08-21; no per-run confirmation is collected.

Usage:

    python scripts/m20_05_resume_qualification.py \
        --host http://127.0.0.1:8188 \
        --host-output-root <supplied host root>/output \
        --canary-root <harness-owned directory> \
        --matrix .planning/260820-M19-08_QUALIFICATION_MATRIX.json \
        --evidence .planning/260821-M20-05_LIVE_EVIDENCE.json

The host must have been started by the maintainer with
`H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1` and `H3_CONTEXT_LATENT_RESUME_CANARY_ROOT` set to
the same `--canary-root`; the harness refuses with a remediation message when the canary
nodes are absent from `/object_info`.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comfyui_h3_context.adapters.joint_av_latent_store import (  # noqa: E402
    PrivateJointAVLatentStore,
)
from comfyui_h3_context.adapters.segment_artifact_store import (  # noqa: E402
    ArtifactInspectionStatus,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint  # noqa: E402
from comfyui_h3_context.core.joint_av_latent import (  # noqa: E402
    build_joint_av_latent_authority,
    decode_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import (  # noqa: E402
    LatentCheckpointBoundary,
    LatentResumeRequest,
    ResumeCompositionNodeRefs,
    admit_latent_checkpoint_resume,
    build_latent_resume_authority,
    build_resume_composition,
)
from comfyui_h3_context.core.temporal_profile import (  # noqa: E402
    AcceptedQualification,
    CapabilityStatus,
    QualifiedRow,
    TemporalCapabilityProfile,
    build_temporal_profile,
)
from comfyui_h3_context.latent_resume_canary import (  # noqa: E402
    CANARY_MARKER_NAME,
    CANARY_MARKER_PAYLOAD,
    CANARY_QUALIFICATION_FILENAME,
    RESUME_LOAD_CANARY_NODE_ID,
    RESUME_SAVE_CANARY_NODE_ID,
)
from scripts.m19_07.live import flac_streaminfo  # noqa: E402
from scripts.m19_08.runs import (  # noqa: E402
    VRAM_STANDARD_BYTES,
    ProbeError,
    ProbeExecutionError,
    WeightDesignation,
    assert_queue_idle,
    free_host,
    queue_prompt,
    vram_snapshot,
    wait_for_history,
)
from scripts.m19_08.runs import _request as host_request  # noqa: E402

EVIDENCE_SCHEMA = "h3.m20_05.resume_qualification.v1"

#: The M19-08 resume-row geometry, unchanged: same probe, same split, same length.
PROBE_WIDTH = 384
PROBE_HEIGHT = 256
TOTAL_STEPS = 4
COMPLETED_STEPS = 2
PROBE_SEED = 1
PROBE_LENGTH = 22
PROBE_PROMPT = "a plain gray studio wall"
RUN_TIMEOUT_SECONDS = 1800.0

ARTIFACT_ID = "m20-05-resume-checkpoint-001"
SOURCE_ID = "segment.resume"
OUTPUT_PREFIX = "m20_05_resume_b"

EXIT_BLOCKED = 3


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _derive_projection(matrix: dict[str, Any]) -> dict[str, Any]:
    """Project the accepted matrix onto the content-free consume-time qualification form."""

    subject = matrix["subject"]["native_source"]["sha256"]
    rows = [
        {key: row[key] for key in ("row", "status", "reason_code", "consumer")}
        for row in matrix["rows"]
    ]
    return {"subject_identity": subject, "rows": rows}


def _profile_from_projection(projection: dict[str, Any]) -> TemporalCapabilityProfile:
    accepted = AcceptedQualification(
        subject_identity=projection["subject_identity"],
        rows=tuple(
            QualifiedRow(
                row=entry["row"],
                status=CapabilityStatus(entry["status"]),
                reason_code=entry["reason_code"],
                consumer=entry["consumer"],
            )
            for entry in projection["rows"]
        ),
    )
    return build_temporal_profile(accepted)


def _prepare_canary_root(root: Path, projection: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / CANARY_MARKER_NAME).write_text(CANARY_MARKER_PAYLOAD, encoding="utf-8")
    (root / CANARY_QUALIFICATION_FILENAME).write_text(
        json.dumps(projection, sort_keys=True, indent=1), encoding="utf-8"
    )


def _cleanup_canary_root(root: Path) -> None:
    store = root / "store"
    if store.is_dir():
        shutil.rmtree(store)
    for name in (CANARY_QUALIFICATION_FILENAME, CANARY_MARKER_NAME):
        target = root / name
        if target.is_file():
            target.unlink()


def _assert_canaries_registered(base_url: str) -> None:
    info = host_request(base_url, "/object_info", timeout=120.0)
    if not isinstance(info, dict):
        raise ProbeError("the host object inventory could not be read")
    missing = [
        node_id
        for node_id in (RESUME_SAVE_CANARY_NODE_ID, RESUME_LOAD_CANARY_NODE_ID)
        if node_id not in info
    ]
    if missing:
        raise ProbeError(
            "the resume canary nodes are not registered on the host; start the host with "
            "H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1 and H3_CONTEXT_LATENT_RESUME_CANARY_ROOT "
            "set to the harness --canary-root, then rerun (missing: " + ", ".join(missing) + ")"
        )


def _identity_fingerprints(base_url: str, designation: WeightDesignation) -> dict[str, str]:
    stats = host_request(base_url, "/system_stats")
    system = stats.get("system") if isinstance(stats, dict) else None
    if not isinstance(system, dict):
        raise ProbeError("the host system identity could not be read")
    runtime_facts = {
        key: str(system.get(key))
        for key in ("comfyui_version", "pytorch_version", "python_version")
    }
    return {
        "model_fingerprint": canonical_fingerprint(
            {"schema": "h3.m20_05.model_identity.v1", "designation": designation.as_evidence()}
        ),
        "runtime_fingerprint": canonical_fingerprint(
            {"schema": "h3.m20_05.runtime_identity.v1", **runtime_facts}
        ),
        "settings_fingerprint": canonical_fingerprint(
            {
                "schema": "h3.m20_05.settings_identity.v1",
                "width": PROBE_WIDTH,
                "height": PROBE_HEIGHT,
                "length": PROBE_LENGTH,
                "steps": TOTAL_STEPS,
                "seed": PROBE_SEED,
                "scheduler": "simple",
                "sampler": "res_multistep",
                "prompt": PROBE_PROMPT,
            }
        ),
    }


def _base_graph(designation: WeightDesignation) -> dict[str, Any]:
    return {
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
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": designation.video_vae}},
        "av": {"class_type": "VAELoader", "inputs": {"vae_name": designation.audio_vae}},
        "h3": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["c", 0],
                "vae": ["vv", 0],
                "prompt": PROBE_PROMPT,
                "width": PROBE_WIDTH,
                "height": PROBE_HEIGHT,
                "length": PROBE_LENGTH,
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
    }


def _boundary_inputs(boundary: LatentCheckpointBoundary) -> dict[str, int]:
    return {
        "total_steps": boundary.total_steps,
        "completed_steps": boundary.completed_steps,
        "seed": boundary.seed,
        "requested_frames": boundary.requested_frames,
    }


def _workflow_a(
    designation: WeightDesignation,
    boundary: LatentCheckpointBoundary,
    identity: dict[str, str],
    transaction_fingerprint: str,
) -> dict[str, Any]:
    """Stage-1 of the qualified split schedule feeding the save canary; nothing else."""

    prompt = _base_graph(designation)
    prompt["sigmas"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": ["u", 0],
            "scheduler": "simple",
            "steps": boundary.total_steps,
            "denoise": 1,
        },
    }
    prompt["split"] = {
        "class_type": "SplitSigmas",
        "inputs": {"sigmas": ["sigmas", 0], "step": boundary.completed_steps},
    }
    prompt["noise"] = {
        "class_type": "RandomNoise",
        "inputs": {"noise_seed": boundary.seed},
    }
    prompt["stage1"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["noise", 0],
            "guider": ["guider", 0],
            "sampler": ["sampler", 0],
            "sigmas": ["split", 0],
            "latent_image": ["h3", 1],
        },
    }
    prompt["save_ckpt"] = {
        "class_type": RESUME_SAVE_CANARY_NODE_ID,
        "inputs": {
            "samples": ["stage1", 0],
            "artifact_id": ARTIFACT_ID,
            "transaction_fingerprint": transaction_fingerprint,
            **_boundary_inputs(boundary),
            **identity,
            "source_id": SOURCE_ID,
        },
    }
    return prompt


def _workflow_b(
    designation: WeightDesignation,
    boundary: LatentCheckpointBoundary,
    identity: dict[str, str],
    receipt_wire: str,
) -> dict[str, Any]:
    """Load canary plus the pure resume fragment; no stage-1 sampler exists at all."""

    prompt = _base_graph(designation)
    prompt["load_ckpt"] = {
        "class_type": RESUME_LOAD_CANARY_NODE_ID,
        "inputs": {
            "checkpoint_receipt": receipt_wire,
            **_boundary_inputs(boundary),
            **identity,
            "source_id": SOURCE_ID,
        },
    }
    fragment = build_resume_composition(
        boundary,
        ResumeCompositionNodeRefs(
            model_node="u",
            guider_node="guider",
            sampler_select_node="sampler",
            latent_source_node="load_ckpt",
        ),
        scheduler_name="simple",
    )
    prompt.update(fragment)
    prompt["decode_v"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["resume_stage2", 0], "vae": ["vv", 0]},
    }
    prompt["decode_a"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": ["resume_stage2", 0], "vae": ["av", 0]},
    }
    prompt["save_v"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["decode_v", 0], "filename_prefix": f"{OUTPUT_PREFIX}/frame"},
    }
    prompt["save_a"] = {
        "class_type": "SaveAudio",
        "inputs": {"audio": ["decode_a", 0], "filename_prefix": f"{OUTPUT_PREFIX}/audio"},
    }
    return prompt


def _structural_pins(prompt: dict[str, Any]) -> dict[str, Any]:
    """Assert and record the node-level skip topology before anything is queued."""

    samplers = [
        name for name, node in prompt.items() if node["class_type"] == "SamplerCustomAdvanced"
    ]
    if samplers != ["resume_stage2"]:
        raise ProbeError("workflow B must contain exactly the resume stage-2 sampler")
    stage2 = prompt["resume_stage2"]["inputs"]
    if stage2["sigmas"] != ["resume_split", 1]:
        raise ProbeError("workflow B stage-2 must consume the split low tail")
    if any(node["class_type"] == "RandomNoise" for node in prompt.values()):
        raise ProbeError("workflow B must not draw fresh noise")
    if prompt["resume_noise"]["class_type"] != "DisableNoise":
        raise ProbeError("workflow B must resume under DisableNoise")
    return {
        "sampler_nodes": samplers,
        "stage2_sigmas": stage2["sigmas"],
        "stage2_latent_source": stage2["latent_image"],
        "noise_class": prompt["resume_noise"]["class_type"],
        "split_step": prompt["resume_split"]["inputs"]["step"],
        "schedule_steps": prompt["resume_sigmas"]["inputs"]["steps"],
    }


def _expected_audio_samples(frames: int) -> int:
    exact = Fraction(frames * 4000, 3)
    return math.ceil(exact / 800) * 800


def _collect_outputs(output_root: Path) -> tuple[int, dict[str, Any]]:
    probe_dir = output_root / OUTPUT_PREFIX
    if not probe_dir.is_dir():
        raise ProbeError("workflow B wrote no output directory")
    frames = 0
    audio: dict[str, Any] | None = None
    for item in sorted(probe_dir.iterdir()):
        if not item.is_file():
            continue
        if item.suffix.lower() == ".png":
            frames += 1
        elif item.suffix.lower() == ".flac":
            audio = flac_streaminfo(item.read_bytes()).as_evidence()
    if audio is None:
        raise ProbeError("workflow B produced no audio stream")
    return frames, audio


def _remove_outputs(output_root: Path) -> None:
    probe_dir = output_root / OUTPUT_PREFIX
    if probe_dir.is_dir():
        shutil.rmtree(probe_dir)


def _run(
    base_url: str, prompt: dict[str, Any], *, wait_outputs: bool = True
) -> tuple[dict[str, Any], float, dict[str, Any]]:
    assert_queue_idle(base_url)
    before = vram_snapshot(base_url)
    if before.total_bytes > VRAM_STANDARD_BYTES:
        raise ProbeError("device total exceeds the 96 GB standard; refusing to run")
    started = time.monotonic()
    prompt_id = queue_prompt(base_url, prompt)
    entry = wait_for_history(base_url, prompt_id, timeout_seconds=RUN_TIMEOUT_SECONDS)
    elapsed = round(time.monotonic() - started, 1)
    after = vram_snapshot(base_url)
    return (
        entry,
        elapsed,
        {
            "before": before.as_evidence(),
            "after_run": after.as_evidence(),
            "within_96gb_standard": after.total_bytes <= VRAM_STANDARD_BYTES,
        },
    )


def _ui_output(entry: dict[str, Any], node: str, key: str) -> str:
    outputs = entry.get("outputs") or {}
    values = (outputs.get(node) or {}).get(key)
    if not isinstance(values, list) or not values or not isinstance(values[0], str):
        raise ProbeError(f"the host history carries no {key} from {node}")
    return values[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--host-output-root", required=True)
    parser.add_argument("--canary-root", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    base_url: str = args.host
    output_root = Path(args.host_output_root)
    canary_root = Path(args.canary_root)
    if not output_root.is_dir():
        print("the supplied host output root does not exist", file=sys.stderr)
        return EXIT_BLOCKED

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    projection = _derive_projection(matrix)
    profile = _profile_from_projection(projection)
    descriptor_authority = build_joint_av_latent_authority(profile)
    resume_authority = build_latent_resume_authority(profile)

    designation = WeightDesignation(
        video_model="H3\\minimax_h3_fl2va_bf16.safetensors",
        text_encoder="qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        video_vae="minimax_h3_video_vae_fp16.safetensors",
        audio_vae="minimax_h3_audio_vae_fp32.safetensors",
    )
    boundary = LatentCheckpointBoundary(
        total_steps=TOTAL_STEPS,
        completed_steps=COMPLETED_STEPS,
        seed=PROBE_SEED,
        requested_frames=PROBE_LENGTH,
    )
    identity = _identity_fingerprints(base_url, designation)
    transaction_fingerprint = canonical_fingerprint(
        {"schema": "h3.m20_05.transaction.v1", "artifact_id": ARTIFACT_ID}
    )

    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "subject": matrix["subject"],
        "authorization": (
            "standing per maintainer directive 2026-08-21: M19-08 weight designation and "
            "96 GB ceiling confirmation apply without per-run asking"
        ),
        "designation": designation.as_evidence(),
        "probe": {
            "width": PROBE_WIDTH,
            "height": PROBE_HEIGHT,
            "length": PROBE_LENGTH,
            "steps": TOTAL_STEPS,
            "split_step": COMPLETED_STEPS,
            "seed": PROBE_SEED,
            "scheduler": "simple",
            "sampler": "res_multistep",
        },
        "boundary": boundary.to_wire(),
        "identity_fingerprints": identity,
        "oracle": "structural_resume_executes_no_bitexact_oracle",
    }

    try:
        # Root preparation and registration probing sit inside the cleanup scope so an
        # aborted precondition never strands marker/projection files (hygiene on every path).
        _prepare_canary_root(canary_root, projection)
        _assert_canaries_registered(base_url)

        # Workflow A: stage-1 plus save canary -> COMPLETE checkpoint receipt.
        workflow_a = _workflow_a(designation, boundary, identity, transaction_fingerprint)
        entry_a, elapsed_a, vram_a = _run(base_url, workflow_a)
        receipt_wire = _ui_output(entry_a, "save_ckpt", "checkpoint_receipt")
        receipt = decode_joint_av_latent_receipt(receipt_wire.encode("utf-8"))
        if receipt.execution_fingerprint != boundary.fingerprint:
            raise ProbeError("the checkpoint does not stand at the declared boundary")
        evidence["workflow_a"] = {
            "elapsed_seconds": elapsed_a,
            "vram": vram_a,
            "receipt": json.loads(receipt_wire),
        }

        # Script-side truth: the real store re-inspection and the real admission decision.
        store = PrivateJointAVLatentStore(canary_root / "store", clock_ms=_now_ms)
        inspection = store.inspect(receipt)
        if inspection.status is not ArtifactInspectionStatus.REUSABLE:
            raise ProbeError(
                "the checkpoint is not reusable at the harness canary root; the host was "
                "likely started with a different H3_CONTEXT_LATENT_RESUME_CANARY_ROOT"
            )
        request = LatentResumeRequest(
            descriptor_authority_fingerprint=descriptor_authority.fingerprint,
            boundary=boundary,
            model_fingerprint=identity["model_fingerprint"],
            runtime_fingerprint=identity["runtime_fingerprint"],
            settings_fingerprint=identity["settings_fingerprint"],
            source_id=SOURCE_ID,
            predecessor_artifact_fingerprint=None,
            now_ms=_now_ms(),
        )
        decision = admit_latent_checkpoint_resume(
            authority=resume_authority,
            descriptor_authority=descriptor_authority,
            request=request,
            receipt=receipt,
        )
        if not decision.admitted:
            raise ProbeError("script-side admission refused: " + ",".join(decision.reason_codes))
        evidence["script_side_admission"] = decision.to_wire()

        # Workflow B: load canary -> in-host admission -> pure resume fragment -> outputs.
        workflow_b = _workflow_b(designation, boundary, identity, receipt_wire)
        evidence["structural_pins"] = _structural_pins(workflow_b)
        entry_b, elapsed_b, vram_b = _run(base_url, workflow_b)
        host_decision = json.loads(_ui_output(entry_b, "load_ckpt", "decision"))
        frames, audio = _collect_outputs(output_root)
        expected_samples = _expected_audio_samples(PROBE_LENGTH)
        evidence["workflow_b"] = {
            "elapsed_seconds": elapsed_b,
            "vram": vram_b,
            "host_admission": host_decision,
            "frames_decoded": frames,
            "frames_exact": frames == PROBE_LENGTH,
            "audio": audio,
            "audio_expected_samples": expected_samples,
            "audio_exact": audio["total_samples"] == expected_samples,
        }
        if frames != PROBE_LENGTH or audio["total_samples"] != expected_samples:
            raise ProbeError("resumed generation missed the exact extents rule")

        # Negative control: one diverged identity domain must refuse with its reason code.
        _remove_outputs(output_root)
        negative_identity = dict(identity)
        negative_identity["settings_fingerprint"] = canonical_fingerprint(
            {"schema": "h3.m20_05.settings_identity.v1", "negated": True}
        )
        workflow_neg = _workflow_b(designation, boundary, negative_identity, receipt_wire)
        try:
            _run(base_url, workflow_neg)
        except ProbeExecutionError as refusal:
            facts = refusal.facts
            if "settings_mismatch" not in facts.get("exception_message", ""):
                raise ProbeError(
                    "the negative control failed without the classified reason code"
                ) from refusal
            evidence["negative_control"] = {
                "diverged_domain": "settings_fingerprint",
                "outcome": "refused_with_reason_code",
                "facts": facts,
            }
        else:
            raise ProbeError("the negative control was not refused")
    finally:
        free_host(base_url)
        _remove_outputs(output_root)
        evidence["hygiene"] = {
            "freed": True,
            "outputs_removed": True,
            "after_free": vram_snapshot(base_url).as_evidence(),
        }
        if not args.keep_root:
            _cleanup_canary_root(canary_root)
            evidence["hygiene"]["canary_root_emptied"] = True

    Path(args.evidence).write_text(
        json.dumps(evidence, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    print(f"M20-05 resume qualification complete; evidence at {args.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
