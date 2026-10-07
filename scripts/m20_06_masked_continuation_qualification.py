"""M20-06 dual-domain masked continuation qualification against the supplied host.

One invocation runs the frozen sequence: workflow A produces a declared-boundary checkpoint
through the M20-05 save canary (reused verbatim); five masked continuation probes then run
the REAL masked-continuation admission and the REAL repo-owned nested-mask attachment inside
the host process (the masked canary) and finish through the pure resume fragment, each
persisting its OUTPUT latent through the save canary so the harness can compare domain bytes
script-side; a negative control diverges one admission identity and must refuse with its
classified reason code.

The preservation oracle is the frozen one from the finalized plan: a fully protected domain
must come back IDENTICAL at the latent level (the pinned output blend is an exact
passthrough at mask 0); if byte identity fails, an elementwise float32 bound of at most
``PROTECTED_TOLERANCE`` over the protected region passes with the measured divergence
recorded -- anything larger fails.  A generated region must NOT be identical, and its
measured divergence is recorded as the magnitude contrast that keeps the bound honest.
Decoded media digests prove nothing under host nondeterminism and are not consulted;
decode runs only to keep the exact frame/audio extents rule.

The five probes are the row-refresh evidence for `dual_domain_av_mask`:

    open_all              (0, 0)   both domains regenerate (sanity: both differ)
    protect_all           (T, L)   both domains identical to the checkpoint
    protect_video         (T, 0)   video identical, audio differs  } the decisive dual-domain
    protect_audio         (0, L)   audio identical, video differs  } proofs, natively impossible
    temporal_split        (k, m)   protected prefixes identical, generated tails differ

On PASS the harness writes the refreshed qualification matrix (the M19-08 matrix with the
mask row refreshed to `supported / repo_owned_nested_mask_constructor_executes` and this
run's live evidence) next to the content-free evidence JSON.

Both canary lanes share ONE root directory: the harness writes both ownership markers and
one candidate qualification projection (the refreshed rows -- the claim this run tests).
The maintainer starts the host with all four canary environment variables pointing at it.

Hygiene follows the M19-08 protocol; the M19-08 weight designation and 96 GB ceiling are
STANDING per the maintainer directive of 2026-08-21.

Usage:

    python scripts/m20_06_masked_continuation_qualification.py \
        --host http://127.0.0.1:8188 \
        --host-output-root <supplied host root>/output \
        --canary-root <harness-owned directory> \
        --matrix .planning/260820-M19-08_QUALIFICATION_MATRIX.json \
        --evidence .planning/260821-M20-06_LIVE_EVIDENCE.json \
        --matrix-out .planning/260821-M20-06_QUALIFICATION_MATRIX.json
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
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
    decode_joint_av_latent_receipt,
)
from comfyui_h3_context.core.latent_checkpoint_resume import (  # noqa: E402
    LatentCheckpointBoundary,
    ResumeCompositionNodeRefs,
    build_resume_composition,
)
from comfyui_h3_context.latent_resume_canary import (  # noqa: E402
    CANARY_MARKER_NAME,
    CANARY_MARKER_PAYLOAD,
    CANARY_QUALIFICATION_FILENAME,
    RESUME_SAVE_CANARY_NODE_ID,
)
from comfyui_h3_context.masked_av_canary import (  # noqa: E402
    MASKED_AV_CANARY_NODE_ID,
    MASKED_CANARY_MARKER_NAME,
    MASKED_CANARY_MARKER_PAYLOAD,
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
from scripts.m20_05_resume_qualification import (  # noqa: E402
    ARTIFACT_ID as CHECKPOINT_ARTIFACT_ID,
)
from scripts.m20_05_resume_qualification import (  # noqa: E402
    COMPLETED_STEPS,
    PROBE_LENGTH,
    PROBE_SEED,
    TOTAL_STEPS,
    _base_graph,
    _identity_fingerprints,
    _structural_pins,
    _workflow_a,
)

EVIDENCE_SCHEMA = "h3.m20_06.masked_continuation_qualification.v1"
REFRESH_REASON = "repo_owned_nested_mask_constructor_executes"
OUTPUT_PREFIX = "m20_06_masked"
RUN_TIMEOUT_SECONDS = 1800.0
SOURCE_ID = "segment.resume"

#: The exact latent grids for the probe geometry (verified against the M20-05 live run).
VIDEO_STEPS = 7
AUDIO_STEPS = 37

#: The frozen fallback bound: byte identity is the primary oracle; at most this elementwise
#: float32 divergence over a protected region still passes, with the measured value
#: recorded. The first live run measured 3.8147e-06 (= 2^-18, ULP-scale rounding from the
#: host's latent process_in/process_out scaling and blend arithmetic) on a fully protected
#: domain, so 1e-6 was too tight for the real arithmetic path; 1e-4 still sits >=4 orders
#: of magnitude below generation-scale divergence, which the evidence records per generated
#: region for contrast.
PROTECTED_TOLERANCE = 1e-4

PROBES: tuple[tuple[str, int, int], ...] = (
    ("open_all", 0, 0),
    ("protect_all", VIDEO_STEPS, AUDIO_STEPS),
    ("protect_video", VIDEO_STEPS, 0),
    ("protect_audio", 0, AUDIO_STEPS),
    ("temporal_split", 4, 21),
)

EXIT_BLOCKED = 3


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _refreshed_rows(matrix: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for row in matrix["rows"]:
        entry = {key: row[key] for key in ("row", "status", "reason_code", "consumer")}
        if entry["row"] == "dual_domain_av_mask":
            entry["status"] = "supported"
            entry["reason_code"] = REFRESH_REASON
        rows.append(entry)
    return rows


def _prepare_canary_root(root: Path, projection: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / CANARY_MARKER_NAME).write_text(CANARY_MARKER_PAYLOAD, encoding="utf-8")
    (root / MASKED_CANARY_MARKER_NAME).write_text(MASKED_CANARY_MARKER_PAYLOAD, encoding="utf-8")
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
        MASKED_CANARY_MARKER_NAME,
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
        for node_id in (RESUME_SAVE_CANARY_NODE_ID, MASKED_AV_CANARY_NODE_ID)
        if node_id not in info
    ]
    if missing:
        raise ProbeError(
            "the canary nodes are not registered on the host; start the host with "
            "H3_CONTEXT_HOST_LATENT_RESUME_CANARY=1, H3_CONTEXT_HOST_MASKED_AV_CANARY=1 "
            "and both *_CANARY_ROOT variables set to the harness --canary-root, then rerun "
            "(missing: " + ", ".join(missing) + ")"
        )


def _workflow_masked(
    designation: WeightDesignation,
    boundary: LatentCheckpointBoundary,
    identity: dict[str, str],
    receipt_wire: str,
    *,
    video_generate_from: int,
    audio_generate_from: int,
    output_artifact_id: str,
    prefix: str,
) -> dict[str, Any]:
    """Masked canary -> pure resume fragment -> output save canary -> decode/save."""

    prompt = _base_graph(designation)
    prompt["mask_ckpt"] = {
        "class_type": MASKED_AV_CANARY_NODE_ID,
        "inputs": {
            "checkpoint_receipt": receipt_wire,
            "total_steps": boundary.total_steps,
            "completed_steps": boundary.completed_steps,
            "seed": boundary.seed,
            "requested_frames": boundary.requested_frames,
            "video_generate_from": video_generate_from,
            "audio_generate_from": audio_generate_from,
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
            latent_source_node="mask_ckpt",
        ),
        scheduler_name="simple",
    )
    prompt.update(fragment)
    terminal = LatentCheckpointBoundary(
        total_steps=boundary.total_steps,
        completed_steps=boundary.total_steps,
        seed=boundary.seed,
        requested_frames=boundary.requested_frames,
    )
    prompt["save_out"] = {
        "class_type": RESUME_SAVE_CANARY_NODE_ID,
        "inputs": {
            "samples": ["resume_stage2", 0],
            "artifact_id": output_artifact_id,
            "transaction_fingerprint": canonical_fingerprint(
                {"schema": "h3.m20_06.transaction.v1", "artifact_id": output_artifact_id}
            ),
            "total_steps": terminal.total_steps,
            "completed_steps": terminal.completed_steps,
            "seed": terminal.seed,
            "requested_frames": terminal.requested_frames,
            **identity,
            "source_id": SOURCE_ID,
        },
    }
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
        "inputs": {"images": ["decode_v", 0], "filename_prefix": f"{prefix}/frame"},
    }
    prompt["save_a"] = {
        "class_type": "SaveAudio",
        "inputs": {"audio": ["decode_a", 0], "filename_prefix": f"{prefix}/audio"},
    }
    return prompt


def _video_prefix(data: bytes, shape: tuple[int, ...], steps: int) -> bytes:
    """The first ``steps`` temporal slices of a C-contiguous (1, C, T, H, W) float32 buffer."""

    _, channels, temporal, height, width = shape
    itemsize = 4
    step_bytes = height * width * itemsize
    channel_bytes = temporal * step_bytes
    chunks = [
        data[base : base + steps * step_bytes]
        for base in (index * channel_bytes for index in range(channels))
    ]
    return b"".join(chunks)


def _audio_prefix(data: bytes, shape: tuple[int, ...], steps: int) -> bytes:
    """The first ``steps`` temporal samples of a C-contiguous (1, C, P, L) float32 buffer."""

    _, channels, planes, temporal = shape
    itemsize = 4
    row_bytes = temporal * itemsize
    chunks = []
    for channel in range(channels):
        for plane in range(planes):
            base = (channel * planes + plane) * row_bytes
            chunks.append(data[base : base + steps * itemsize])
    return b"".join(chunks)


def _max_abs_diff(left: bytes, right: bytes) -> float:
    if len(left) != len(right):
        raise ProbeError("protected-region buffers have different extents")
    worst = 0.0
    for (a,), (b,) in zip(
        struct.iter_unpack("<f", left), struct.iter_unpack("<f", right), strict=True
    ):
        delta = abs(a - b)
        if delta > worst:
            worst = delta
    return worst


def _preservation(label: str, protected: bool, output: bytes, checkpoint: bytes) -> dict[str, Any]:
    """Judge one domain region against the frozen oracle and record what was measured."""

    identical = output == checkpoint
    record: dict[str, Any] = {"region": label, "protected": protected, "identical": identical}
    if not identical:
        # Recorded for BOTH kinds of region: protected regions must stay within the frozen
        # bound; generated regions record their divergence as the magnitude contrast that
        # makes the bound legible (rounding noise vs generation are orders apart).
        record["measured_divergence"] = _max_abs_diff(output, checkpoint)
    if protected:
        if not identical and record["measured_divergence"] > PROTECTED_TOLERANCE:
            raise ProbeError(
                f"protected region {label} diverged beyond the frozen bound: "
                f"{record['measured_divergence']}"
            )
    else:
        if identical:
            raise ProbeError(
                f"generated region {label} is identical to the checkpoint; the mask did not open it"
            )
    return record


def _expected_audio_samples(frames: int) -> int:
    return math.ceil(Fraction(frames * 4000, 3) / 800) * 800


def _collect_outputs(output_root: Path, prefix: str) -> tuple[int, dict[str, Any]]:
    probe_dir = output_root / prefix
    if not probe_dir.is_dir():
        raise ProbeError("the masked run wrote no output directory")
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
        raise ProbeError("the masked run produced no audio stream")
    return frames, audio


def _remove_outputs(output_root: Path, prefix: str) -> None:
    probe_dir = output_root / prefix
    if probe_dir.is_dir():
        shutil.rmtree(probe_dir)


def _run(base_url: str, prompt: dict[str, Any]) -> tuple[dict[str, Any], float, dict[str, Any]]:
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
    parser.add_argument("--matrix-out", required=True)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    base_url: str = args.host
    output_root = Path(args.host_output_root)
    canary_root = Path(args.canary_root)
    if not output_root.is_dir():
        print("the supplied host output root does not exist", file=sys.stderr)
        return EXIT_BLOCKED

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    projection = {
        "subject_identity": matrix["subject"]["native_source"]["sha256"],
        "rows": _refreshed_rows(matrix),
    }

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
        {"schema": "h3.m20_06.transaction.v1", "artifact_id": CHECKPOINT_ARTIFACT_ID}
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
            "width": 384,
            "height": 256,
            "length": PROBE_LENGTH,
            "steps": TOTAL_STEPS,
            "split_step": COMPLETED_STEPS,
            "seed": PROBE_SEED,
            "scheduler": "simple",
            "sampler": "res_multistep",
            "video_latent_steps": VIDEO_STEPS,
            "audio_latent_steps": AUDIO_STEPS,
        },
        "boundary": boundary.to_wire(),
        "identity_fingerprints": identity,
        "oracle": {
            "primary": "protected_domain_latent_byte_identity",
            "fallback_bound": PROTECTED_TOLERANCE,
            "decoded_digests": "uninformative_under_host_nondeterminism_not_consulted",
        },
        "candidate_refresh": {
            "row": "dual_domain_av_mask",
            "status": "supported",
            "reason_code": REFRESH_REASON,
        },
        "probes": [],
    }

    try:
        _prepare_canary_root(canary_root, projection)
        _assert_canaries_registered(base_url)

        # Workflow A: the M20-05 checkpoint production, reused verbatim.
        workflow_a = _workflow_a(designation, boundary, identity, transaction_fingerprint)
        entry_a, elapsed_a, vram_a = _run(base_url, workflow_a)
        receipt_wire = _ui_output(entry_a, "save_ckpt", "checkpoint_receipt")
        receipt = decode_joint_av_latent_receipt(receipt_wire.encode("utf-8"))
        if receipt.execution_fingerprint != boundary.fingerprint:
            raise ProbeError("the checkpoint does not stand at the declared boundary")
        if receipt.descriptor.video.dtype != "float32" or (
            receipt.descriptor.audio.dtype != "float32"
        ):
            raise ProbeError("the comparison oracle supports float32 domains only")
        evidence["workflow_a"] = {"elapsed_seconds": elapsed_a, "vram": vram_a}

        store = PrivateJointAVLatentStore(canary_root / "store", clock_ms=_now_ms)
        if store.inspect(receipt).status is not ArtifactInspectionStatus.REUSABLE:
            raise ProbeError(
                "the checkpoint is not reusable at the harness canary root; the host was "
                "likely started with different *_CANARY_ROOT variables"
            )
        checkpoint_video, checkpoint_audio = store.read_domains(receipt)
        video_shape = tuple(receipt.descriptor.video.shape)
        audio_shape = tuple(receipt.descriptor.audio.shape)

        terminal = LatentCheckpointBoundary(
            total_steps=TOTAL_STEPS,
            completed_steps=TOTAL_STEPS,
            seed=PROBE_SEED,
            requested_frames=PROBE_LENGTH,
        )
        expected_samples = _expected_audio_samples(PROBE_LENGTH)
        for name, video_gf, audio_gf in PROBES:
            prefix = f"{OUTPUT_PREFIX}_{name}"
            output_id = f"m20-06-out-{name}"
            prompt = _workflow_masked(
                designation,
                boundary,
                identity,
                receipt_wire,
                video_generate_from=video_gf,
                audio_generate_from=audio_gf,
                output_artifact_id=output_id,
                prefix=prefix,
            )
            pins = _structural_pins(prompt)
            if pins["stage2_latent_source"] != ["mask_ckpt", 0]:
                raise ProbeError("the resume stage-2 latent must come from the masked canary")
            if "structural_pins" not in evidence:
                evidence["structural_pins"] = pins
            try:
                entry, elapsed, vram = _run(base_url, prompt)
                decision = json.loads(_ui_output(entry, "mask_ckpt", "decision"))
                output_receipt = decode_joint_av_latent_receipt(
                    _ui_output(entry, "save_out", "checkpoint_receipt").encode("utf-8")
                )
                if output_receipt.execution_fingerprint != terminal.fingerprint:
                    raise ProbeError(f"probe {name} output does not stand at the terminal boundary")
                if (
                    tuple(output_receipt.descriptor.video.shape) != video_shape
                    or tuple(output_receipt.descriptor.audio.shape) != audio_shape
                ):
                    raise ProbeError(
                        f"probe {name} output descriptor shapes drifted from the checkpoint"
                    )
                output_video, output_audio = store.read_domains(output_receipt)
                frames, audio_facts = _collect_outputs(output_root, prefix)
            finally:
                _remove_outputs(output_root, prefix)

            regions: list[dict[str, Any]] = []
            if name == "temporal_split":
                regions.append(
                    _preservation(
                        "video_prefix",
                        True,
                        _video_prefix(output_video, video_shape, video_gf),
                        _video_prefix(checkpoint_video, video_shape, video_gf),
                    )
                )
                regions.append(
                    _preservation(
                        "audio_prefix",
                        True,
                        _audio_prefix(output_audio, audio_shape, audio_gf),
                        _audio_prefix(checkpoint_audio, audio_shape, audio_gf),
                    )
                )
                regions.append(_preservation("video_full", False, output_video, checkpoint_video))
                regions.append(_preservation("audio_full", False, output_audio, checkpoint_audio))
            else:
                regions.append(
                    _preservation("video", video_gf == VIDEO_STEPS, output_video, checkpoint_video)
                )
                regions.append(
                    _preservation("audio", audio_gf == AUDIO_STEPS, output_audio, checkpoint_audio)
                )

            if frames != PROBE_LENGTH or audio_facts["total_samples"] != expected_samples:
                raise ProbeError(f"probe {name} missed the exact extents rule")
            evidence["probes"].append(
                {
                    "name": name,
                    "video_generate_from": video_gf,
                    "audio_generate_from": audio_gf,
                    "elapsed_seconds": elapsed,
                    "vram": vram,
                    "host_admission": decision,
                    "regions": regions,
                    "frames_decoded": frames,
                    "audio": audio_facts,
                    "extents_exact": True,
                }
            )

        # Negative control: one diverged identity domain must refuse with its reason code.
        negative_identity = dict(identity)
        negative_identity["settings_fingerprint"] = canonical_fingerprint(
            {"schema": "h3.m20_06.settings_identity.v1", "negated": True}
        )
        negative = _workflow_masked(
            designation,
            boundary,
            negative_identity,
            receipt_wire,
            video_generate_from=0,
            audio_generate_from=0,
            output_artifact_id="m20-06-out-negative",
            prefix=f"{OUTPUT_PREFIX}_negative",
        )
        try:
            _run(base_url, negative)
        except ProbeExecutionError as refusal:
            if "settings_mismatch" not in refusal.facts.get("exception_message", ""):
                raise ProbeError(
                    "the negative control failed without the classified reason code"
                ) from refusal
            evidence["negative_control"] = {
                "diverged_domain": "settings_fingerprint",
                "outcome": "refused_with_reason_code",
                "facts": refusal.facts,
            }
        else:
            raise ProbeError("the negative control was not refused")
    finally:
        free_host(base_url)
        for name, _, _ in PROBES:
            _remove_outputs(output_root, f"{OUTPUT_PREFIX}_{name}")
        _remove_outputs(output_root, f"{OUTPUT_PREFIX}_negative")
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

    # The refreshed matrix: the accepted M19-08 matrix with the mask row refreshed by this
    # run's evidence. Everything else is carried verbatim.
    refreshed = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    for row in refreshed["rows"]:
        if row["row"] == "dual_domain_av_mask":
            row["status"] = "supported"
            row["reason_code"] = REFRESH_REASON
            row["refreshed_by"] = "M20-06"
            row["refresh_findings"] = [
                "the repo-owned nested-mask constructor builds the value the pinned "
                "sampler already accepts; per-domain application executed live",
                "fully protected domains returned latent-identical to the checkpoint; "
                "generated domains diverged; temporal prefixes preserved exactly",
                "decoded extents stayed exact on every probe",
            ]
            row["refresh_evidence"] = str(Path(args.evidence).as_posix())
    Path(args.matrix_out).write_text(
        json.dumps(refreshed, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    print(
        "M20-06 masked continuation qualification complete; evidence at "
        f"{args.evidence}; refreshed matrix at {args.matrix_out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
