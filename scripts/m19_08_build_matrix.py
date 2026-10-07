"""Build the M19-08 refreshed qualification matrix from the live evidence record.

Reproducible and auditable: the per-row verdicts and findings live here as reviewed text; the
live facts come only from the evidence JSON the probe executor wrote; the subject block is
re-derived from the supplied host and the pinned native source at build time, so a drifted
subject fails the build instead of publishing a matrix about something else.

Usage:

    python scripts/m19_08_build_matrix.py \
        --host http://127.0.0.1:8188/ \
        --native-source <supplied host root>/comfy_extras/nodes_minimax_h3.py \
        --evidence .planning/260820-M19-08_LIVE_EVIDENCE.json \
        --output .planning/260820-M19-08_QUALIFICATION_MATRIX.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comfyui_h3_context.core import native_h3  # noqa: E402
from scripts.m19_07.host import node_inventory, opaque_identity, system_snapshot  # noqa: E402

EXIT_BLOCKED = 3

_NATIVE_MODULE_PREFIXES = ("nodes", "comfy_extras.")


def _mask_exhaustiveness(object_info: dict[str, Any]) -> dict[str, Any]:
    """Turn "no native nested-mask constructor" into affirmative evidence.

    The sampler reads the noise mask from the latent dict; the only native writer is
    SetLatentNoiseMask, whose input type is the dense MASK. The inventory below is every
    native node that emits MASK or accepts MASK while emitting LATENT — the complete set of
    candidate constructors/appliers on the live host. None of them produces or accepts a
    nested (per-domain) mask value.
    """
    inventory: list[str] = []
    for node_id in sorted(object_info):
        spec = object_info[node_id]
        module = str(spec.get("python_module", ""))
        if module != "nodes" and not module.startswith("comfy_extras."):
            continue
        outputs = tuple(spec.get("output") or ())
        inputs: set[str] = set()
        for kind in ("required", "optional"):
            for value in (spec.get("input", {}).get(kind) or {}).values():
                if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
                    inputs.add(value[0])
        emits_mask = "MASK" in outputs
        applies_mask_to_latent = "MASK" in inputs and "LATENT" in outputs
        if emits_mask or applies_mask_to_latent:
            inventory.append(f"{node_id} ({module})")
    if not inventory:
        raise RuntimeError(
            "live inventory produced no MASK-surface nodes; refusing to claim absence"
        )
    return {
        "source_identity": (
            "ComfyUI "
            + native_h3.NATIVE_H3_HOST_REVISION[:12]
            + " comfy/samplers.py + nodes_minimax_h3.py gitblob:"
            + native_h3.NATIVE_H3_SOURCE_BLOB[:12]
        ),
        "candidate_mechanisms": [
            "SolidMask -> SetLatentNoiseMask dense mask (probed live: sampler applies it "
            "to the video domain only)",
            "sampler nested-mask branch applies one mask per domain, but no native node "
            "constructs a nested mask value",
            "every native MASK producer/applier on the live host emits or accepts dense "
            "masks only (inventory below)",
        ],
        "live_node_inventory": inventory,
        "inventory_source": (
            "/object_info of the supplied host at matrix build time, filtered to native modules "
            "(python_module == 'nodes' or 'comfy_extras.*'), nodes emitting MASK or "
            "applying MASK to LATENT"
        ),
    }


ROWS: tuple[dict[str, Any], ...] = (
    {
        "row": "joint_av_latent_descriptor",
        "consumer": "M20-04",
        "status": "supported",
        "reason_code": "descriptor_measured_live",
        "findings": [
            "the joint AV latent is a NestedTensor pair from construction: the host's native "
            "SaveLatent fails with an AttributeError on the empty latent (recorded live in "
            "this row), and the pinned factory allocates the pair directly; the same failure "
            "on a sampled latent was observed once during the aborted first launch and is "
            "recorded in the command log, not as row-scoped live evidence",
            "layout per the pinned source: video [B,24,T,H/16,W/16] and audio [B,32,2,T40] "
            "at 40 latent fps; the two domains do not share a rank",
            "native persistence is unavailable on this subject; M20-04 must own its "
            "serializer (per-domain sub-tensor decomposition is sufficient: the pair is two "
            "dense tensors)",
            "the extents rule is confirmed weight-backed at three lengths: decoded frames == "
            "requested length exactly, and audio total samples == frames * 4000/3 rounded up "
            "to the 800-sample (25 ms) boundary at 32 kHz — agreeing with the M19-07 "
            "weight-free canaries and the repo-derived temporal profile",
            "tensor dtype is not observable over the HTTP surface; an in-process consumer "
            "reads it directly from the pair (recorded as a limitation, not a blocker)",
        ],
    },
    {
        "row": "completed_boundary_resume",
        "consumer": "M20-05",
        "status": "supported",
        "reason_code": "structural_resume_executes_no_bitexact_oracle",
        "findings": [
            "the control M19-07 lacked: two byte-identical single-pass runs produced "
            "different output digests, so this host's generation is nondeterministic across "
            "identical runs",
            "a follow-up weight-free control showed VAE decode alone is also "
            "nondeterministic; no bit-exact output oracle exists anywhere in this pipeline",
            "M19-07's live_probe_diverged is thereby explained rather than reproduced away: "
            "a resume divergence cannot be attributed to the resume composition when the "
            "single pass does not reproduce itself",
            "the split-schedule resume composition (SplitSigmas step=2, stage-2 DisableNoise "
            "continuing from the stage-1 trajectory output) executes successfully and "
            "delivers the exact requested frame count; the audio-extent rule is confirmed "
            "live only in the descriptor row, not re-measured here",
            "consequence for M20-05: resume acceptance criteria must be structural or "
            "tolerance-based; bit-exact resume verification is impossible on this subject",
        ],
    },
    {
        "row": "dual_domain_av_mask",
        "consumer": "M20-06",
        "status": "unsupported",
        "reason_code": "no_native_nested_mask_constructor",
        "findings": [
            "bounded search: the native latent-mask mechanism (SolidMask -> "
            "SetLatentNoiseMask) plus the pinned sampler's mask application; community "
            "packs are outside the claim",
            "SetLatentNoiseMask is execution-compatible with the H3 NestedTensor: unmasked, "
            "all-open and all-protect probes all executed",
            "the pinned sampler applies a single dense mask to the FIRST nested domain "
            "(video) only and pads every further domain with torch.ones, leaving audio "
            "fully open",
            "the sampler natively accepts a nested denoise mask and applies one mask per "
            "domain — the dual-domain mechanism exists below the node graph",
            "no native node constructs a nested mask, so dual-domain masking is unreachable "
            "from a workflow; M20-06 can reach it with a repo-owned nested-mask constructor "
            "node (it would only build the value the sampler already accepts; no host "
            "patching)",
            "digest-equality discriminators were run and are recorded, but under host "
            "nondeterminism they carry no information and none of this row's conclusions "
            "rest on them",
        ],
    },
    {
        "row": "two_ended_av_bridge",
        "consumer": "M20-07",
        "status": "supported",
        "reason_code": "pixel_domain_two_ended_composition_executes",
        "findings": [
            "the official two-ended composition works: fl2va with both keyframes (two "
            "synthetic solid frames) executed end to end within the 96 GB standard and "
            "delivered the exact requested frame count; the audio-extent rule is confirmed "
            "live only in the descriptor row, not re-measured here",
            "pinned source semantics: first_frame is pinned at resolved_frame_index 0 and "
            "last_frame at frame_count-1; keyframe condition latents are re-injected every "
            "sampling step and never denoised",
            "keyframe anchoring is video-only — keyframes are vae.encode(image) and no "
            "audio anchor input exists; reference audio (ref2va) is advisory conditioning, "
            "not an authority",
            "no latent-domain bridge composition exists natively: the stock dense "
            "combinators reject the joint latent (LatentInterpolate: TypeError "
            "linalg_vector_norm on NestedTensor; LatentConcat: TypeError expected Tensor)",
            "consequence for M20-07: build the bridge on keyframe conditioning; "
            "master-audio authority must be repo-owned, since the native surface offers no "
            "audio anchor",
        ],
    },
)

MATRIX_LIMITATIONS = [
    "this host's generation pipeline is nondeterministic end to end (sampler and VAE decode "
    "both fail an identical-rerun digest comparison), so no consumer may use bit-exact output "
    "equality as an acceptance oracle against this subject; equivalence claims must be "
    "structural or tolerance-based",
    "the latent pair's dtype is not observable over the HTTP surface and is therefore not "
    "asserted by this matrix; in-process consumers read it directly",
    "all weight-backed runs stayed within the 96 GB standard authorized 2026-08-20, closing "
    "the M19-07 ceiling-breach limitation; the measured cross-row peak is 94.97 GiB "
    "(101971500056 bytes used, during the fl2va bridge run); runs at or under the standard "
    "are in budget by definition",
    "the temporal_profile row is carried forward unchanged from the accepted M19-07 matrix; "
    "M19-08 re-measured only the four previously unqualified rows",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--native-source", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--predecessor-matrix", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source_path = Path(args.native_source)
    source_bytes = source_path.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(source_bytes) + source_bytes).hexdigest()  # noqa: S324 - git blob id, not a security hash
    if blob != native_h3.NATIVE_H3_SOURCE_BLOB:
        print("BLOCKED: the supplied native source is not the frozen subject")
        return EXIT_BLOCKED

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    rows_evidence = evidence.get("rows", {})
    missing = [row["row"] for row in ROWS if row["row"] not in rows_evidence]
    if missing:
        print(f"BLOCKED: no partial matrix — unmeasured rows: {missing}")
        return EXIT_BLOCKED

    snapshot = system_snapshot(args.host)
    if snapshot.comfyui_version != native_h3.NATIVE_H3_HOST_VERSION:
        print("BLOCKED: the supplied host is not the frozen subject version")
        return EXIT_BLOCKED

    object_info = dict(
        node_inventory(
            args.host,
            cache=Path(args.evidence).parent / "260820-M19-08_OBJECT_INFO_CACHE.json",
        )
    )
    exhaustiveness = _mask_exhaustiveness(object_info)

    designation = rows_evidence[ROWS[0]["row"]]["designation"]
    for row in ROWS[1:]:
        if rows_evidence[row["row"]]["designation"] != designation:
            print("BLOCKED: rows were measured under different weight designations")
            return EXIT_BLOCKED
    predecessor = json.loads(Path(args.predecessor_matrix).read_text(encoding="utf-8"))
    carried = [row for row in predecessor["rows"] if row["row"] == "temporal_profile"]
    if len(carried) != 1 or carried[0]["status"] != "supported":
        print("BLOCKED: the predecessor temporal_profile row is not the accepted supported row")
        return EXIT_BLOCKED
    # Carried forward unchanged: M19-08 re-measured only the four previously unqualified
    # rows; the accepted temporal_profile row remains the standing result.
    matrix_rows = list(carried)
    for row in ROWS:
        live = rows_evidence[row["row"]]
        matrix_rows.append(
            {
                "row": row["row"],
                "consumer": row["consumer"],
                "status": row["status"],
                "reason_code": row["reason_code"],
                "findings": row["findings"],
                **({"exhaustiveness": exhaustiveness} if row["status"] == "unsupported" else {}),
                "live_evidence": live,
                "source_evidence": {
                    "native_source": native_h3.NATIVE_H3_SOURCE,
                    "native_source_blob": "gitblob:" + native_h3.NATIVE_H3_SOURCE_BLOB,
                    "sampler_mask_application": "comfy/samplers.py CFGGuider.sample "
                    "(dense mask -> first nested domain; ones-padding for the rest; nested "
                    "mask branch applies per domain)",
                },
            }
        )

    def _commit() -> str:
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except (subprocess.SubprocessError, OSError):
            return "unavailable"
        return result.stdout.strip()

    matrix = {
        "schema": "h3.m19_08_qualification_evidence.v1",
        "version": "1.0.0",
        "subject": {
            "item": "M19-08",
            "host": snapshot.as_evidence(),
            "host_state": "probed_read_only_plus_authorized_weight_backed_runs",
            "frozen_host_version": native_h3.NATIVE_H3_HOST_VERSION,
            "frozen_host_revision": native_h3.NATIVE_H3_HOST_REVISION,
            "native_source": {
                "label": native_h3.NATIVE_H3_SOURCE,
                "blob": "gitblob:" + blob,
                "sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
                "byte_length": len(source_bytes),
            },
            "model_subject": [
                opaque_identity(value, prefix="weight")
                for value in (
                    designation["video_model"],
                    designation["text_encoder"],
                    designation["video_vae"],
                    designation["audio_vae"],
                )
            ],
            "project_commit": _commit(),
        },
        "rows": matrix_rows,
        "limitations": MATRIX_LIMITATIONS,
    }

    import jsonschema  # noqa: PLC0415 - optional validation dependency, imported at use

    schema = json.loads(
        (_REPO_ROOT / "scripts" / "m19_08" / "qualification_evidence_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(matrix, schema)

    output = Path(args.output)
    output.write_text(
        json.dumps(matrix, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "rows": {row["row"]: row["status"] for row in ROWS},
                "output": output.name,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
