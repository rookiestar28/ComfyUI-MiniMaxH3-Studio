"""The five qualification rows, assembled from source facts and the live node inventory.

Only one of the five can be decided without loading a weight, and saying so plainly is the job.
The temporal contract is computed by the node source before any model participates, so it is
qualifiable statically with a recorded reason. The other four depend on what the model and the
sampler actually do with a joint latent, and the honest offline outcome for them is `unqualified`
with the native-surface finding recorded — not a guess dressed as a result.

Where an absence *is* decidable offline it is still recorded as a finding rather than promoted to
`unsupported`, because the exhaustiveness a `unsupported` row owes covers the composed graph, not
only the native node surface. A node pack elsewhere in the live inventory could supply a mechanism
the native module does not, and that possibility is only closed by the live canaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .evidence import ROW_CONSUMERS, Exhaustiveness, Row, RowStatus
from .source_facts import SourceFacts
from .temporal import temporal_evidence

#: Native inputs that would have to exist for each successor mechanism to be expressible directly on
#: the pinned H3 nodes. Enumerated rather than searched loosely, so an absence claim can name
#: what it looked for.
CANDIDATE_INPUTS: Mapping[str, tuple[str, ...]] = {
    "completed_boundary_resume": ("latent", "samples", "start_step", "resume", "checkpoint"),
    "dual_domain_av_mask": ("mask", "video_mask", "audio_mask", "noise_mask"),
    "two_ended_av_bridge": ("context", "master_audio", "audio", "left_latent", "right_latent"),
}

#: Node-identity terms searched across the whole live inventory, so an absence claim covers more
#: than the four native classes.
CANDIDATE_NODE_TERMS: Mapping[str, tuple[str, ...]] = {
    "completed_boundary_resume": ("resume", "checkpoint", "start_at_step", "advanced"),
    "dual_domain_av_mask": ("noisemask", "setlatentnoise", "mask"),
    "two_ended_av_bridge": ("bridge", "interpolat", "inbetween", "transition"),
}


def _temporal_row(facts: SourceFacts, live: Mapping[str, Any] | None = None) -> Row:
    evidence = temporal_evidence(facts)
    agreement = evidence["repository_agreement"]
    join = evidence["audio_join"]

    findings: list[str] = []
    if agreement["agrees"]:
        findings.append(
            "comfyui_h3_context.core.length agrees with the pinned source over every declared "
            f"frame count ({agreement['checked_frame_counts']} checked); M20-01 inherits the "
            "lattice unchanged"
        )
    else:
        findings.extend(f"lattice disagreement: {item}" for item in agreement["disagreements"])

    findings.append(
        f"the audio latent join is {join['ratio']} per frame and is exact only every "
        f"{join['exact_period_frames']} frames, so an exact audiovisual boundary is a separate "
        "decision from run validity"
    )
    if join["float_matches_exact"]:
        findings.append(
            "the source evaluates the join in binary floating point while the repository authority "
            "uses Decimal; no lattice member comes closer than "
            f"{join['minimum_tie_distance']} to a rounding tie, and both agree on every member, "
            "so the representations cannot diverge"
        )
    findings.append(
        "the trained-range claim in the source tooltip is documentation, not a measurement; which "
        "lengths the model actually accepts remains a live question for the model envelope"
    )

    healthy = (
        agreement["agrees"]
        and evidence["candidate_set_separates_decisions"]
        and evidence["off_grid_rejected"]
        and join["float_matches_exact"]
    )
    if not healthy:
        return Row(
            name="temporal_profile",
            status=RowStatus.UNQUALIFIED,
            reason_code="temporal_derivation_inconsistent",
            consumer=ROW_CONSUMERS["temporal_profile"],
            source_evidence=evidence,
            findings=tuple(findings),
        )

    if live and live.get("temporal"):
        observed = live["temporal"]
        findings.append(
            "live decode on the supplied host reproduced every derived extent exactly: "
            + "; ".join(
                f"length {c['requested_length']} -> {c['decoded_frames']} frames and "
                f"{c['audio']['total_samples']} audio samples at {c['audio']['sample_rate']} Hz "
                f"({c['stream_delta_milliseconds']:+.4f} ms audio-versus-video)"
                for c in observed["canaries"]
            )
        )
        findings.append(
            "the rounded join is physical, not notional: the audio stream over-runs the video "
            "stream by the rounding residue whenever the frame count is not a multiple of the "
            "join period, so a successor that assumes the two streams share a duration is wrong "
            "for two frame counts out of every three"
        )
        return Row(
            name="temporal_profile",
            status=RowStatus.SUPPORTED,
            reason_code="derived_from_exact_source_and_confirmed_live",
            consumer=ROW_CONSUMERS["temporal_profile"],
            source_evidence=evidence,
            live_evidence=observed,
            findings=tuple(findings),
        )

    return Row(
        name="temporal_profile",
        status=RowStatus.SUPPORTED,
        reason_code="derived_from_exact_source",
        consumer=ROW_CONSUMERS["temporal_profile"],
        source_evidence=evidence,
        live_applicability=(
            "live evidence is not applicable to this row: the frame lattice, the video latent "
            "extent and the audio latent join are computed by the pinned node source itself, in "
            "the latent factory that allocates zero tensors before any weight, sampler or VAE "
            "participates. No model parameter enters the arithmetic, so a weight-backed run could "
            "not change the result. The separate question of which lengths the model envelope "
            "accepts belongs to the live canaries and is recorded as a finding, not folded in here."
        ),
        findings=tuple(findings),
    )


def _latent_row(facts: SourceFacts, live: Mapping[str, Any] | None = None) -> Row:
    video = facts.stream("video")
    audio = facts.stream("audio")
    evidence: dict[str, Any] = {
        "streams": [
            {
                "name": stream.name,
                "rank": stream.rank,
                "channels": stream.channels,
                "dims": list(stream.dims),
            }
            for stream in facts.latent_streams
        ],
        "paired": video is not None and audio is not None,
    }
    findings = [
        "the joint latent is allocated as a pair in one factory, so the descriptor is structural "
        "rather than a convention a caller could vary",
    ]
    if video is not None and audio is not None:
        findings.append(
            f"video carries {video.channels} channels over {video.rank} axes and audio "
            f"{audio.channels} channels over {audio.rank} axes; the two do not share a rank, so a "
            "serializer cannot treat them as one homogeneous tensor"
        )
    findings.append(
        "dtype and device are absent from the source: the factory allocates with the runtime's "
        "default and the encoded latents come from the VAE, so a stable dtype claim and a safe "
        "serialization candidate both require the live canary"
    )

    probes = list((live or {}).get("probes", []))
    serialization = [p for p in probes if p["mechanism"] == "latent_serialization"]
    for probe in serialization:
        findings.append(
            f"the stock serialization candidate fails on this subject: {probe['node_type']} raised "
            f"{probe.get('exception_type')} because the joint latent is a nested pair rather than "
            "one tensor. M20-04 cannot assume the existing latent store format and must own a "
            "serializer, which is a cost this qualification exists to surface before the item is "
            "planned rather than after"
        )

    return Row(
        name="joint_av_latent_descriptor",
        status=RowStatus.UNQUALIFIED,
        reason_code="live_descriptor_not_yet_measured",
        consumer=ROW_CONSUMERS["joint_av_latent_descriptor"],
        source_evidence=evidence,
        live_evidence={"probes": probes} if probes else None,
        findings=tuple(findings),
    )


def _absent_from_native_surface(
    facts: SourceFacts, row_name: str
) -> tuple[bool, tuple[str, ...], dict[str, Any]]:
    """Whether any native H3 node exposes an input that could express the mechanism."""
    wanted = CANDIDATE_INPUTS[row_name]
    hits: list[str] = []
    for node in facts.node_classes:
        for item in node.inputs:
            if any(needle in item.name.lower() for needle in wanted):
                hits.append(f"{node.node_id}.{item.name}")
    evidence = {
        "searched_inputs": list(wanted),
        "native_nodes": list(facts.node_ids()),
        "matching_native_inputs": sorted(hits),
    }
    return not hits, tuple(sorted(hits)), evidence


def _verdict_outran_the_search(
    verdict: Mapping[str, Any], computed_candidates: Sequence[str]
) -> tuple[bool, str]:
    """Refuse an absence claim that is narrower than the harness's own search.

    This exists because the first `unsupported` row this harness emitted was wrong in exactly this
    way: the row carried a hand-authored exhaustiveness record naming six nodes while the same row's
    `source_evidence` carried the 361 candidates the harness had itself found, four of them named
    for the very mechanism the row declared absent. The row even carried its own generated finding
    saying the absence was "not yet exhaustive", directly beside `status: unsupported`.

    `enforce_status_rules` could not catch it, because the record was structurally complete: every
    field was populated. Completeness is not consistency. An absence claim has to account for what
    the search actually turned up, either by having examined a candidate or by explicitly ruling it
    out, and anything left over degrades the row.
    """
    accounted = set(verdict.get("live_node_inventory", ())) | set(verdict.get("ruled_out", ()))
    unaccounted = sorted(c for c in computed_candidates if c not in accounted)
    if not unaccounted:
        return False, ""
    shown = ", ".join(unaccounted[:5])
    more = f" and {len(unaccounted) - 5} more" if len(unaccounted) > 5 else ""
    return True, (
        f"downgraded: the absence claim named {len(accounted)} node(s) but the harness's own "
        f"search of the live inventory returned {len(computed_candidates)} candidate(s) that the "
        f"claim neither examined nor ruled out, including {shown}{more}"
    )


def _unqualified_reason(probes: Sequence[Mapping[str, Any]]) -> str:
    """Name why a row stayed unqualified, distinguishing a failed probe from an unattempted one.

    Collapsing these would be the same mistake as collapsing absence into missing measurement: a
    mechanism that was driven and did not work is a much stronger signal than one nobody tried, and
    a successor planning against this matrix needs to know which it is looking at.
    """
    if any(p.get("outcome") == "diverged" for p in probes):
        return "live_probe_diverged"
    if any(p.get("outcome") == "rejected" for p in probes):
        return "live_probe_rejected"
    if probes:
        return "live_probe_inconclusive"
    return "not_probed_composition_unmeasured"


def _probes_for(live: Mapping[str, Any] | None, row_name: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(p for p in (live or {}).get("probes", []) if p.get("mechanism") == row_name)


def _successor_row(
    facts: SourceFacts,
    row_name: str,
    inventory_terms_found: Sequence[str],
    *,
    inventory_available: bool,
    extra_findings: Sequence[str] = (),
    probes: Sequence[Mapping[str, Any]] = (),
    blocked_reason: str | None = None,
    verdict: Mapping[str, Any] | None = None,
) -> Row:
    absent, hits, evidence = _absent_from_native_surface(facts, row_name)
    evidence["live_inventory_candidates"] = list(inventory_terms_found)
    evidence["live_inventory_available"] = inventory_available

    findings: list[str] = []
    if absent:
        findings.append(
            f"no native H3 node exposes any of {', '.join(CANDIDATE_INPUTS[row_name])}; the "
            "mechanism cannot be expressed directly on the pinned nodes"
        )
    else:
        findings.append(f"native inputs that could express the mechanism: {', '.join(hits)}")

    # An empty candidate list means two completely different things, and reporting them the same
    # way would turn "we did not look" into "there is nothing there" -- exactly the false negative
    # this item's status rules exist to prevent.
    if not inventory_available:
        findings.append(
            "the live node inventory was not read on this run, so composition through generic "
            "nodes is entirely unmeasured; no absence claim about the wider host is possible from "
            "this evidence"
        )
    elif inventory_terms_found:
        findings.append(
            "the live inventory does contain generic candidates "
            f"({', '.join(inventory_terms_found[:6])}"
            f"{', ...' if len(inventory_terms_found) > 6 else ''}), so composition through generic "
            "nodes is not excluded and the absence is not yet exhaustive"
        )
    else:
        findings.append(
            "the live inventory was read and contains no generic candidate either, but whether "
            "composition could still express the mechanism remains unmeasured until the live "
            "canary runs"
        )
    for probe in probes:
        if probe["outcome"] == "accepted":
            findings.append(
                f"{probe['node_type']} accepted the joint latent without error, but attaching a "
                "mechanism is not the same as the sampler honouring it; whether it has any effect "
                "is a separate question this stage did not reach"
            )
        else:
            findings.append(
                f"{probe['node_type']} was rejected by the subject: {probe.get('exception_type')}"
            )
    if blocked_reason and not probes:
        findings.append(blocked_reason)
    findings.extend(extra_findings)

    if verdict and verdict.get("status") == RowStatus.UNSUPPORTED.value:
        outran, why = _verdict_outran_the_search(verdict, inventory_terms_found)
        if outran:
            return Row(
                name=row_name,
                status=RowStatus.UNQUALIFIED,
                reason_code="unsupported_claim_outran_the_search",
                consumer=ROW_CONSUMERS[row_name],
                source_evidence=evidence,
                live_evidence={"probes": list(probes), "rejected_verdict": dict(verdict)},
                findings=(*findings, *verdict.get("findings", ()), why),
            )
        findings.extend(verdict.get("findings", ()))
        return Row(
            name=row_name,
            status=RowStatus.UNSUPPORTED,
            reason_code=verdict["reason_code"],
            consumer=ROW_CONSUMERS[row_name],
            source_evidence=evidence,
            live_evidence={"probes": list(probes), "verdict": dict(verdict)},
            exhaustiveness=Exhaustiveness(
                source_identity=facts.identity.blob,
                candidate_mechanisms=tuple(verdict["candidate_mechanisms"]),
                live_node_inventory=tuple(verdict["live_node_inventory"]),
                inventory_source=verdict["inventory_source"],
            ),
            findings=tuple(findings),
        )

    return Row(
        name=row_name,
        status=RowStatus.UNQUALIFIED,
        reason_code=_unqualified_reason(probes),
        consumer=ROW_CONSUMERS[row_name],
        source_evidence=evidence,
        live_evidence={"probes": list(probes)} if probes else None,
        findings=tuple(findings),
    )


def build_rows(
    facts: SourceFacts,
    *,
    inventory_candidates: Mapping[str, Sequence[str]],
    inventory_available: bool,
    live: Mapping[str, Any] | None = None,
) -> tuple[Row, ...]:
    bridge_extra = (
        "MiniMaxH3ImageToVideo does accept optional first_frame and last_frame keyframes resolved "
        "to the first and last frame indices. That is two-ended keyframe conditioning and is not "
        "the two-ended audiovisual bridge M20-07 requires, which also needs an existing "
        "audiovisual context admitted at both boundaries, a generated middle interval and one "
        "explicit master-audio authority. This row must never be promoted on keyframe evidence.",
    )
    return (
        _temporal_row(facts, live),
        _latent_row(facts, live),
        _successor_row(
            facts,
            "completed_boundary_resume",
            inventory_candidates.get("completed_boundary_resume", ()),
            inventory_available=inventory_available,
            probes=_probes_for(live, "completed_boundary_resume"),
            blocked_reason=(live or {}).get("resource_block"),
            verdict=(live or {}).get("verdicts", {}).get("completed_boundary_resume"),
        ),
        _successor_row(
            facts,
            "dual_domain_av_mask",
            inventory_candidates.get("dual_domain_av_mask", ()),
            inventory_available=inventory_available,
            probes=_probes_for(live, "dual_domain_av_mask"),
            blocked_reason=(live or {}).get("resource_block"),
            verdict=(live or {}).get("verdicts", {}).get("dual_domain_av_mask"),
        ),
        _successor_row(
            facts,
            "two_ended_av_bridge",
            inventory_candidates.get("two_ended_av_bridge", ()),
            inventory_available=inventory_available,
            extra_findings=bridge_extra,
            probes=_probes_for(live, "two_ended_av_bridge"),
            blocked_reason=(live or {}).get("resource_block"),
            verdict=(live or {}).get("verdicts", {}).get("two_ended_av_bridge"),
        ),
    )


def exhaustiveness_for(
    facts: SourceFacts, row_name: str, inventory: Sequence[str], inventory_source: str
) -> Exhaustiveness:
    """Build the record an `unsupported` row would need, once the live canary closes composition."""
    return Exhaustiveness(
        source_identity=facts.identity.blob,
        candidate_mechanisms=CANDIDATE_INPUTS[row_name],
        live_node_inventory=tuple(inventory),
        inventory_source=inventory_source,
    )
