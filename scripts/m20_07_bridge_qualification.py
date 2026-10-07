"""M20-07 two-ended AV bridge qualification against the supplied host.

One invocation runs the frozen sequence: two full 90-frame t2va segment runs persist their
terminal joint-AV latents through the M20-05 save canary (left seed 1, right seed 2, shared
settings identity); the bridge probe then runs the REAL bridge admission, the REAL byte-exact
composition (left 51-frame tail context, zero middle, right 51-frame head context, per
domain) and the REAL interval-mask attachment inside the host process (the bridge canary),
generates the 39-frame middle through the fresh full-schedule fragment under fl2va
two-keyframe conditioning (two synthetic solid frames, the supported row's probe form), and
persists the 141-frame output through the save canary with the left boundary declared as
predecessor.  Two negative controls then diverge one admission identity each (a competing
right-boundary master-audio declaration; an identical receipt on both ends) and must refuse
with their classified reason codes.

The frozen oracle (finalized plan D8) under host nondeterminism (no bit-exact or
digest-difference comparisons anywhere):

    preserved left context   output video [0,15) / audio [0,85)     == left tail slices
    preserved right context  output video [27,42) / audio [150,235) == right head slices
    generated middle         output video [15,27) / audio [85,150)  != its zero init
    extents                  141 decoded frames, 188000 audio samples, exact
    refusals                 classified reason codes on both negative controls

Byte identity is primary; a protected region may fall back to an elementwise float32 bound
of at most ``PROTECTED_TOLERANCE`` (the M20-06 live-calibrated 1e-4) with the measured value
recorded, and every generated region's divergence is recorded as the magnitude contrast.

Probe geometry: 384x256 primary; on OOM rerun with ``--width 320 --height 192`` (documented
fallback, same frame plan).  The M19-08 weight designation and the 96 GB ceiling are
STANDING per the maintainer directives of 2026-08-20/21.

The run needs BOTH canary lanes at ONE shared root: start the host with
H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1, H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY=1 and both
*_CANARY_ROOT variables pointing at the harness ``--canary-root``.

Usage:

    python scripts/m20_07_bridge_qualification.py \
        --host http://127.0.0.1:8188 \
        --host-output-root <supplied host root>/output \
        --canary-root <harness-owned directory> \
        --matrix .planning/260821-M20-06_QUALIFICATION_MATRIX.json \
        --evidence .planning/260821-M20-07_LIVE_EVIDENCE.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comfyui_h3_context.adapters.comfyui_two_ended_bridge import (  # noqa: E402
    bridge_domain_shapes,
)
from comfyui_h3_context.adapters.joint_av_latent_store import (  # noqa: E402
    PrivateJointAVLatentStore,
)
from comfyui_h3_context.adapters.segment_artifact_store import (  # noqa: E402
    ArtifactInspectionStatus,
)
from comfyui_h3_context.core.canonical import canonical_fingerprint  # noqa: E402
from comfyui_h3_context.core.joint_av_latent import (  # noqa: E402
    JointAVLatentReceipt,
    build_joint_av_latent_authority,
    decode_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import (  # noqa: E402
    LatentCheckpointBoundary,
    ResumeCompositionNodeRefs,
)
from comfyui_h3_context.core.masked_av_continuation import (  # noqa: E402
    build_masked_av_authority,
)
from comfyui_h3_context.core.two_ended_av_bridge import (  # noqa: E402
    MasterAudioDeclaration,
    admit_two_ended_bridge,
    build_bridge_composition,
    build_two_ended_bridge_authority,
    build_two_ended_bridge_receipt,
    plan_bridge_mask,
    plan_two_ended_bridge,
)
from comfyui_h3_context.latent_resume_canary import (  # noqa: E402
    CANARY_MARKER_NAME,
    CANARY_MARKER_PAYLOAD,
    CANARY_QUALIFICATION_FILENAME,
    RESUME_SAVE_CANARY_NODE_ID,
)
from comfyui_h3_context.two_ended_bridge_canary import (  # noqa: E402
    BRIDGE_CANARY_MARKER_NAME,
    BRIDGE_CANARY_MARKER_PAYLOAD,
    TWO_ENDED_BRIDGE_CANARY_NODE_ID,
)
from scripts.m19_08.runs import (  # noqa: E402
    ProbeError,
    ProbeExecutionError,
    WeightDesignation,
    free_host,
    vram_snapshot,
)
from scripts.m19_08.runs import _request as host_request  # noqa: E402
from scripts.m20_05_resume_qualification import (  # noqa: E402
    _base_graph,
    _derive_projection,
    _profile_from_projection,
)
from scripts.m20_06_masked_continuation_qualification import (  # noqa: E402
    PROTECTED_TOLERANCE,
    _collect_outputs,
    _expected_audio_samples,
    _max_abs_diff,
    _remove_outputs,
    _run,
    _ui_output,
)

EVIDENCE_SCHEMA = "h3.m20_07.bridge_qualification.v1"

#: The frozen probe frame plan (finalized plan D7): 90-frame boundary segments, 51-frame
#: contexts, 39-frame gap, 141-frame produced bridge.  Every number is grid-derived: 51 is
#: the M20-01 context grid, 39 the smallest producible audio-exact gap, and 90 the smallest
#: on-lattice segment whose 51-frame tail boundary is audio-exact.
SEGMENT_LENGTH = 90
CONTEXT_FRAMES = 51
GAP_FRAMES = 39
PRODUCED_LENGTH = CONTEXT_FRAMES + GAP_FRAMES + CONTEXT_FRAMES

TOTAL_STEPS = 4
LEFT_SEED = 1
RIGHT_SEED = 2
BRIDGE_SEED = 3
PROBE_PROMPT = "a plain gray studio wall"

MASTER_SOURCE_ID = "master.audio"
LEFT_END_FRAME = SEGMENT_LENGTH
RIGHT_START_FRAME = LEFT_END_FRAME + GAP_FRAMES
COVERAGE_START = 0
COVERAGE_END = LEFT_END_FRAME - CONTEXT_FRAMES + PRODUCED_LENGTH

LEFT_ARTIFACT_ID = "m20-07-bridge-left-001"
RIGHT_ARTIFACT_ID = "m20-07-bridge-right-001"
OUTPUT_ARTIFACT_ID = "m20-07-bridge-out-001"
SOURCE_ID = "segment.bridge"
OUTPUT_PREFIX = "m20_07_bridge"

EXIT_BLOCKED = 3


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _prepare_canary_root(root: Path, projection: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / CANARY_MARKER_NAME).write_text(CANARY_MARKER_PAYLOAD, encoding="utf-8")
    (root / BRIDGE_CANARY_MARKER_NAME).write_text(BRIDGE_CANARY_MARKER_PAYLOAD, encoding="utf-8")
    (root / CANARY_QUALIFICATION_FILENAME).write_text(
        json.dumps(projection, sort_keys=True, indent=1), encoding="utf-8"
    )


def _cleanup_canary_root(root: Path) -> None:
    store = root / "store"
    if store.is_dir():
        shutil.rmtree(store)
    for name in (
        CANARY_QUALIFICATION_FILENAME,
        CANARY_MARKER_NAME,
        BRIDGE_CANARY_MARKER_NAME,
    ):
        target = root / name
        if target.is_file():
            target.unlink()


def _assert_canaries_registered(base_url: str) -> None:
    info = host_request(base_url, "/object_info", timeout=120.0)
    if not isinstance(info, dict):
        raise ProbeError("the host object inventory could not be read")
    missing = [
        node_id
        for node_id in (RESUME_SAVE_CANARY_NODE_ID, TWO_ENDED_BRIDGE_CANARY_NODE_ID)
        if node_id not in info
    ]
    if missing:
        raise ProbeError(
            "the canary nodes are not registered on the host; start the host with "
            "H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1, "
            "H3_CONTEXT_HOST_TWO_ENDED_BRIDGE_CANARY=1 and both *_CANARY_ROOT variables "
            "set to the harness --canary-root, then rerun (missing: " + ", ".join(missing) + ")"
        )


def _identity_fingerprints(
    base_url: str, designation: WeightDesignation, width: int, height: int
) -> dict[str, str]:
    stats = host_request(base_url, "/system_stats")
    system = stats.get("system") if isinstance(stats, dict) else None
    if not isinstance(system, dict):
        raise ProbeError("the host system identity could not be read")
    runtime_facts = {
        key: str(system.get(key))
        for key in ("comfyui_version", "pytorch_version", "python_version")
    }
    # The per-segment seed is execution state, not settings: both boundaries must share one
    # settings identity or the bridge admission's alignment check would refuse its own probes.
    return {
        "model_fingerprint": canonical_fingerprint(
            {"schema": "h3.m20_07.model_identity.v1", "designation": designation.as_evidence()}
        ),
        "runtime_fingerprint": canonical_fingerprint(
            {"schema": "h3.m20_07.runtime_identity.v1", **runtime_facts}
        ),
        "settings_fingerprint": canonical_fingerprint(
            {
                "schema": "h3.m20_07.settings_identity.v1",
                "width": width,
                "height": height,
                "segment_length": SEGMENT_LENGTH,
                "steps": TOTAL_STEPS,
                "scheduler": "simple",
                "sampler": "res_multistep",
                "prompt": PROBE_PROMPT,
            }
        ),
    }


def _boundary_inputs(boundary: LatentCheckpointBoundary) -> dict[str, int]:
    return {
        "total_steps": boundary.total_steps,
        "completed_steps": boundary.completed_steps,
        "seed": boundary.seed,
        "requested_frames": boundary.requested_frames,
    }


def _segment_boundary(seed: int) -> LatentCheckpointBoundary:
    return LatentCheckpointBoundary(
        total_steps=TOTAL_STEPS,
        completed_steps=TOTAL_STEPS,
        seed=seed,
        requested_frames=SEGMENT_LENGTH,
    )


def _bridge_boundary() -> LatentCheckpointBoundary:
    return LatentCheckpointBoundary(
        total_steps=TOTAL_STEPS,
        completed_steps=TOTAL_STEPS,
        seed=BRIDGE_SEED,
        requested_frames=PRODUCED_LENGTH,
    )


def _workflow_segment(
    designation: WeightDesignation,
    identity: dict[str, str],
    *,
    seed: int,
    artifact_id: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    """One full t2va segment run persisting its terminal latent; no decode, no outputs."""

    prompt = _base_graph(designation)
    prompt["h3"]["inputs"].update({"width": width, "height": height, "length": SEGMENT_LENGTH})
    prompt["sigmas"] = {
        "class_type": "BasicScheduler",
        "inputs": {"model": ["u", 0], "scheduler": "simple", "steps": TOTAL_STEPS, "denoise": 1},
    }
    prompt["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    prompt["seg_sampler"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["noise", 0],
            "guider": ["guider", 0],
            "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0],
            "latent_image": ["h3", 1],
        },
    }
    prompt["save_ckpt"] = {
        "class_type": RESUME_SAVE_CANARY_NODE_ID,
        "inputs": {
            "samples": ["seg_sampler", 0],
            "artifact_id": artifact_id,
            "transaction_fingerprint": canonical_fingerprint(
                {"schema": "h3.m20_07.transaction.v1", "artifact_id": artifact_id}
            ),
            **_boundary_inputs(_segment_boundary(seed)),
            **identity,
            "source_id": SOURCE_ID,
        },
    }
    return prompt


def _workflow_bridge(
    designation: WeightDesignation,
    identity: dict[str, str],
    *,
    left_wire: str,
    right_wire: str,
    left_fingerprint: str,
    width: int,
    height: int,
    output_artifact_id: str = OUTPUT_ARTIFACT_ID,
    prefix: str = OUTPUT_PREFIX,
    right_master_source_id: str = "",
    persist_output: bool = True,
) -> dict[str, Any]:
    """Bridge canary -> fresh full-schedule fragment under fl2va two-keyframe conditioning."""

    prompt = _base_graph(designation)
    prompt["kf_first"] = {
        "class_type": "EmptyImage",
        "inputs": {"width": width, "height": height, "batch_size": 1, "color": 0},
    }
    prompt["kf_last"] = {
        "class_type": "EmptyImage",
        "inputs": {"width": width, "height": height, "batch_size": 1, "color": 16777215},
    }
    prompt["h3"]["inputs"].update(
        {
            "width": width,
            "height": height,
            "length": PRODUCED_LENGTH,
            "first_frame": ["kf_first", 0],
            "last_frame": ["kf_last", 0],
        }
    )
    prompt["bridge_ckpt"] = {
        "class_type": TWO_ENDED_BRIDGE_CANARY_NODE_ID,
        "inputs": {
            "left_receipt": left_wire,
            "right_receipt": right_wire,
            "target_gap_frames": GAP_FRAMES,
            "left_context_frames": CONTEXT_FRAMES,
            "right_context_frames": CONTEXT_FRAMES,
            "left_end_frame": LEFT_END_FRAME,
            "right_start_frame": RIGHT_START_FRAME,
            "left_lookahead_frames": 0,
            "right_lookahead_frames": 0,
            "master_source_id": MASTER_SOURCE_ID,
            "coverage_start_frame": COVERAGE_START,
            "coverage_end_frame": COVERAGE_END,
            "right_master_source_id": right_master_source_id,
        },
    }
    fragment = build_bridge_composition(
        seed=BRIDGE_SEED,
        total_steps=TOTAL_STEPS,
        refs=ResumeCompositionNodeRefs(
            model_node="u",
            guider_node="guider",
            sampler_select_node="sampler",
            latent_source_node="bridge_ckpt",
        ),
        scheduler_name="simple",
    )
    prompt.update(fragment)
    if not persist_output:
        return prompt
    prompt["save_out"] = {
        "class_type": RESUME_SAVE_CANARY_NODE_ID,
        "inputs": {
            "samples": ["bridge_sampler", 0],
            "artifact_id": output_artifact_id,
            "transaction_fingerprint": canonical_fingerprint(
                {"schema": "h3.m20_07.transaction.v1", "artifact_id": output_artifact_id}
            ),
            **_boundary_inputs(_bridge_boundary()),
            **identity,
            "source_id": SOURCE_ID,
            "predecessor_artifact_fingerprint": left_fingerprint,
        },
    }
    prompt["decode_v"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["bridge_sampler", 0], "vae": ["vv", 0]},
    }
    prompt["decode_a"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": ["bridge_sampler", 0], "vae": ["av", 0]},
    }
    prompt["save_v"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["decode_v", 0], "filename_prefix": f"{prefix}/frame"},
    }
    prompt["save_a"] = {
        "class_type": "SaveAudio",
        "inputs": {"audio": ["decode_a", 0], "filename_prefix": f"{prefix}/audio"},
    }
    return prompt


def _bridge_pins(prompt: dict[str, Any]) -> dict[str, Any]:
    """Assert and record the bridge topology before anything is queued."""

    samplers = [
        name for name, node in prompt.items() if node["class_type"] == "SamplerCustomAdvanced"
    ]
    if samplers != ["bridge_sampler"]:
        raise ProbeError("the bridge workflow must contain exactly the bridge sampler")
    sampler_inputs = prompt["bridge_sampler"]["inputs"]
    if sampler_inputs["latent_image"] != ["bridge_ckpt", 0]:
        raise ProbeError("the bridge latent must come from the bridge canary")
    if sampler_inputs["sigmas"] != ["bridge_sigmas", 0]:
        raise ProbeError("the bridge must run the full fresh schedule")
    if prompt["bridge_noise"]["class_type"] != "RandomNoise":
        raise ProbeError("the bridge must draw fresh noise")
    h3_inputs = prompt["h3"]["inputs"]
    if h3_inputs.get("first_frame") != ["kf_first", 0] or (
        h3_inputs.get("last_frame") != ["kf_last", 0]
    ):
        raise ProbeError("the bridge must condition on both keyframes (fl2va form)")
    return {
        "sampler_nodes": samplers,
        "latent_source": sampler_inputs["latent_image"],
        "sigmas": sampler_inputs["sigmas"],
        "noise_class": prompt["bridge_noise"]["class_type"],
        "noise_seed": prompt["bridge_noise"]["inputs"]["noise_seed"],
        "schedule_steps": prompt["bridge_sigmas"]["inputs"]["steps"],
        "keyframe_conditioning": "fl2va_two_keyframes",
        "produced_length": prompt["h3"]["inputs"]["length"],
    }


def _video_window(data: bytes, shape: tuple[int, ...], start: int, stop: int) -> bytes:
    """Temporal slices [start, stop) of a C-contiguous (1, C, T, H, W) float32 buffer."""

    batch, channels, temporal, height, width = shape
    if batch != 1:
        raise ProbeError("the oracle slicers support batch 1 only")
    itemsize = 4
    step_bytes = height * width * itemsize
    channel_bytes = temporal * step_bytes
    return b"".join(
        data[base + start * step_bytes : base + stop * step_bytes]
        for base in (index * channel_bytes for index in range(channels))
    )


def _audio_window(data: bytes, shape: tuple[int, ...], start: int, stop: int) -> bytes:
    """Temporal samples [start, stop) of a C-contiguous (1, C, P, L) float32 buffer."""

    batch, channels, planes, temporal = shape
    if batch != 1:
        raise ProbeError("the oracle slicers support batch 1 only")
    itemsize = 4
    row_bytes = temporal * itemsize
    chunks = []
    for channel in range(channels):
        for plane in range(planes):
            base = (channel * planes + plane) * row_bytes
            chunks.append(data[base + start * itemsize : base + stop * itemsize])
    return b"".join(chunks)


def _region(label: str, protected: bool, output: bytes, reference: bytes) -> dict[str, Any]:
    """Judge one region against the frozen oracle and record what was measured."""

    identical = output == reference
    record: dict[str, Any] = {"region": label, "protected": protected, "identical": identical}
    if not identical:
        record["measured_divergence"] = _max_abs_diff(output, reference)
    if protected:
        if not identical and record["measured_divergence"] > PROTECTED_TOLERANCE:
            raise ProbeError(
                f"preserved region {label} diverged beyond the frozen bound: "
                f"{record['measured_divergence']}"
            )
    elif identical:
        raise ProbeError(f"generated region {label} is identical to its zero initialization")
    return record


def _expect_refusal(
    base_url: str, prompt: dict[str, Any], *, reason_code: str, label: str
) -> dict[str, Any]:
    print(
        f"NOTE: negative control '{label}' runs next and intentionally prints a host-console "
        "error; the refusal is the expected outcome."
    )
    try:
        _run(base_url, prompt)
    except ProbeExecutionError as refusal:
        if reason_code not in refusal.facts.get("exception_message", ""):
            raise ProbeError(
                f"negative control '{label}' failed without the classified reason code "
                f"{reason_code}"
            ) from refusal
        return {
            "control": label,
            "expected_reason_code": reason_code,
            "outcome": "refused_with_reason_code",
            "facts": refusal.facts,
        }
    raise ProbeError(f"negative control '{label}' was not refused")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--host-output-root", required=True)
    parser.add_argument("--canary-root", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--width", type=int, default=384)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    base_url: str = args.host
    output_root = Path(args.host_output_root)
    canary_root = Path(args.canary_root)
    width: int = args.width
    height: int = args.height
    if not output_root.is_dir():
        print("the supplied host output root does not exist", file=sys.stderr)
        return EXIT_BLOCKED

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    projection = _derive_projection(matrix)
    profile = _profile_from_projection(projection)
    descriptor_authority = build_joint_av_latent_authority(profile)
    mask_authority = build_masked_av_authority(profile)
    bridge_authority = build_two_ended_bridge_authority(profile)

    designation = WeightDesignation(
        video_model="H3\\minimax_h3_fl2va_bf16.safetensors",
        text_encoder="qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        video_vae="minimax_h3_video_vae_fp16.safetensors",
        audio_vae="minimax_h3_audio_vae_fp32.safetensors",
    )
    identity = _identity_fingerprints(base_url, designation, width, height)
    master = MasterAudioDeclaration(
        master_source_id=MASTER_SOURCE_ID,
        coverage_start_frame=COVERAGE_START,
        coverage_end_frame=COVERAGE_END,
    )

    evidence: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "subject": matrix["subject"],
        "authorization": (
            "standing per maintainer directives 2026-08-20/21: M19-08 weight designation and "
            "96 GB ceiling confirmation apply without per-run asking"
        ),
        "designation": designation.as_evidence(),
        "row_consumed": {
            "row": "two_ended_av_bridge",
            "status": "supported",
            "reason_code": "pixel_domain_two_ended_composition_executes",
        },
        "probe": {
            "width": width,
            "height": height,
            "segment_length": SEGMENT_LENGTH,
            "context_frames": CONTEXT_FRAMES,
            "gap_frames": GAP_FRAMES,
            "produced_length": PRODUCED_LENGTH,
            "steps": TOTAL_STEPS,
            "seeds": {"left": LEFT_SEED, "right": RIGHT_SEED, "bridge": BRIDGE_SEED},
            "scheduler": "simple",
            "sampler": "res_multistep",
        },
        "master_audio": master.to_wire(),
        "identity_fingerprints": identity,
        "oracle": {
            "primary": "preserved_context_latent_byte_identity",
            "fallback_bound": PROTECTED_TOLERANCE,
            "generated_middle": "must_diverge_from_zero_initialization",
            "decoded_digests": "uninformative_under_host_nondeterminism_not_consulted",
        },
        "segments": {},
        "negative_controls": [],
    }

    try:
        _prepare_canary_root(canary_root, projection)
        _assert_canaries_registered(base_url)
        store = PrivateJointAVLatentStore(canary_root / "store", clock_ms=_now_ms)

        receipts: dict[str, JointAVLatentReceipt] = {}
        wires: dict[str, str] = {}
        for side, seed, artifact_id in (
            ("left", LEFT_SEED, LEFT_ARTIFACT_ID),
            ("right", RIGHT_SEED, RIGHT_ARTIFACT_ID),
        ):
            prompt = _workflow_segment(
                designation,
                identity,
                seed=seed,
                artifact_id=artifact_id,
                width=width,
                height=height,
            )
            entry, elapsed, vram = _run(base_url, prompt)
            wire = _ui_output(entry, "save_ckpt", "checkpoint_receipt")
            receipt = decode_joint_av_latent_receipt(wire.encode("utf-8"))
            if receipt.execution_fingerprint != _segment_boundary(seed).fingerprint:
                raise ProbeError(f"the {side} segment does not stand at its declared boundary")
            if receipt.descriptor.video.dtype != "float32" or (
                receipt.descriptor.audio.dtype != "float32"
            ):
                raise ProbeError("the comparison oracle supports float32 domains only")
            if store.inspect(receipt).status is not ArtifactInspectionStatus.REUSABLE:
                raise ProbeError(
                    f"the {side} segment checkpoint is not reusable at the harness canary "
                    "root; the host was likely started with different *_CANARY_ROOT variables"
                )
            receipts[side] = receipt
            wires[side] = wire
            evidence["segments"][side] = {"elapsed_seconds": elapsed, "vram": vram}

        # Script-side truth: the same plan, mask and admission the canary computes in-host.
        plan = plan_two_ended_bridge(
            left_descriptor=receipts["left"].descriptor,
            right_descriptor=receipts["right"].descriptor,
            descriptor_authority=descriptor_authority,
            master=master,
            target_gap_frames=GAP_FRAMES,
            left_context_frames=CONTEXT_FRAMES,
            right_context_frames=CONTEXT_FRAMES,
            left_end_frame=LEFT_END_FRAME,
            right_start_frame=RIGHT_START_FRAME,
        )
        mask_plan = plan_bridge_mask(plan)
        decision = admit_two_ended_bridge(
            authority=bridge_authority,
            descriptor_authority=descriptor_authority,
            mask_authority=mask_authority,
            plan=plan,
            mask_plan=mask_plan,
            left_receipt=receipts["left"],
            right_receipt=receipts["right"],
            master=master,
            left_master_source_id=MASTER_SOURCE_ID,
            right_master_source_id=MASTER_SOURCE_ID,
            now_ms=_now_ms(),
        )
        if not decision.admitted:
            raise ProbeError(
                "the script-side bridge admission refused: " + ",".join(decision.reason_codes)
            )
        expected_video_shape, expected_audio_shape = bridge_domain_shapes(
            plan, receipts["left"].descriptor
        )

        bridge_prompt = _workflow_bridge(
            designation,
            identity,
            left_wire=wires["left"],
            right_wire=wires["right"],
            left_fingerprint=receipts["left"].fingerprint,
            width=width,
            height=height,
        )
        evidence["structural_pins"] = _bridge_pins(bridge_prompt)
        try:
            entry, elapsed, vram = _run(base_url, bridge_prompt)
            canary_record = json.loads(_ui_output(entry, "bridge_ckpt", "decision"))
            output_receipt = decode_joint_av_latent_receipt(
                _ui_output(entry, "save_out", "checkpoint_receipt").encode("utf-8")
            )
            frames, audio_facts = _collect_outputs(output_root, OUTPUT_PREFIX)
        finally:
            _remove_outputs(output_root, OUTPUT_PREFIX)

        # The in-host decision must be the exact script-side decision, fingerprint for
        # fingerprint -- one admission, computed twice, no drift.
        if canary_record["plan"]["schema"] != plan.to_wire()["schema"] or (
            canary_record["decision"] != decision.to_wire()
        ):
            raise ProbeError("the in-host admission diverged from the script-side admission")
        if output_receipt.execution_fingerprint != _bridge_boundary().fingerprint:
            raise ProbeError("the bridge output does not stand at the terminal boundary")
        if tuple(output_receipt.descriptor.video.shape) != expected_video_shape or (
            tuple(output_receipt.descriptor.audio.shape) != expected_audio_shape
        ):
            raise ProbeError("the bridge output descriptor shapes drifted from the plan")
        bridge_receipt = build_two_ended_bridge_receipt(
            decision=decision, plan=plan, output_receipt=output_receipt
        )

        left_video, left_audio = store.read_domains(receipts["left"])
        right_video, right_audio = store.read_domains(receipts["right"])
        output_video, output_audio = store.read_domains(output_receipt)
        left_video_shape = tuple(receipts["left"].descriptor.video.shape)
        left_audio_shape = tuple(receipts["left"].descriptor.audio.shape)
        right_video_shape = tuple(receipts["right"].descriptor.video.shape)
        right_audio_shape = tuple(receipts["right"].descriptor.audio.shape)

        video_left_steps = plan.left_video_steps
        video_right_start = plan.left_video_steps + plan.middle_video_steps
        video_total = expected_video_shape[2]
        audio_left_steps = plan.left_audio_steps
        audio_right_start = plan.left_audio_steps + plan.middle_audio_steps
        audio_total = expected_audio_shape[3]
        left_video_total = left_video_shape[2]
        left_audio_total = left_audio_shape[3]

        middle_video = _video_window(
            output_video, expected_video_shape, video_left_steps, video_right_start
        )
        middle_audio = _audio_window(
            output_audio, expected_audio_shape, audio_left_steps, audio_right_start
        )
        regions = [
            _region(
                "video_left_context",
                True,
                _video_window(output_video, expected_video_shape, 0, video_left_steps),
                _video_window(
                    left_video,
                    left_video_shape,
                    left_video_total - video_left_steps,
                    left_video_total,
                ),
            ),
            _region(
                "video_right_context",
                True,
                _video_window(output_video, expected_video_shape, video_right_start, video_total),
                _video_window(right_video, right_video_shape, 0, plan.right_video_steps),
            ),
            _region("video_middle", False, middle_video, b"\x00" * len(middle_video)),
            _region(
                "audio_left_context",
                True,
                _audio_window(output_audio, expected_audio_shape, 0, audio_left_steps),
                _audio_window(
                    left_audio,
                    left_audio_shape,
                    left_audio_total - audio_left_steps,
                    left_audio_total,
                ),
            ),
            _region(
                "audio_right_context",
                True,
                _audio_window(output_audio, expected_audio_shape, audio_right_start, audio_total),
                _audio_window(right_audio, right_audio_shape, 0, plan.right_audio_steps),
            ),
            _region("audio_middle", False, middle_audio, b"\x00" * len(middle_audio)),
        ]

        expected_samples = _expected_audio_samples(PRODUCED_LENGTH)
        if frames != PRODUCED_LENGTH or audio_facts["total_samples"] != expected_samples:
            raise ProbeError("the bridge run missed the exact extents rule")
        evidence["bridge"] = {
            "elapsed_seconds": elapsed,
            "vram": vram,
            "host_admission": canary_record,
            "plan": plan.to_wire(),
            "mask_plan": mask_plan.to_wire(),
            "decision": decision.to_wire(),
            "bridge_receipt": bridge_receipt.to_wire(),
            "regions": regions,
            "frames_decoded": frames,
            "audio": audio_facts,
            "extents_exact": True,
        }

        # Negative controls: each diverges exactly one admission identity and must refuse
        # with its classified reason code (host-console errors are expected).
        evidence["negative_controls"].append(
            _expect_refusal(
                base_url,
                _workflow_bridge(
                    designation,
                    identity,
                    left_wire=wires["left"],
                    right_wire=wires["right"],
                    left_fingerprint=receipts["left"].fingerprint,
                    width=width,
                    height=height,
                    right_master_source_id="clip.audio",
                    persist_output=False,
                ),
                reason_code="master_audio_conflict:right",
                label="competing_right_master_audio",
            )
        )
        evidence["negative_controls"].append(
            _expect_refusal(
                base_url,
                _workflow_bridge(
                    designation,
                    identity,
                    left_wire=wires["left"],
                    right_wire=wires["left"],
                    left_fingerprint=receipts["left"].fingerprint,
                    width=width,
                    height=height,
                    persist_output=False,
                ),
                reason_code="boundary_not_distinct",
                label="identical_boundary_receipts",
            )
        )
    finally:
        free_host(base_url)
        _remove_outputs(output_root, OUTPUT_PREFIX)
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
    print(f"M20-07 bridge qualification complete; evidence at {args.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
