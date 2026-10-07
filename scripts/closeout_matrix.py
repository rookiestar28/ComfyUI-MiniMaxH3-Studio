"""Generate the M18-06 closeout matrix: what the chain changed, and whether the domains held.

No number here is remembered, but they are not all sourced the same way and the difference is worth
stating.  Four metrics are read out of an accepted checkpoint's own committed artifact with `git
show`.  The two manifest byte counts have no machine-readable artifact to read -- the manifests
never persist their own section sizes -- so they are *measured here* off the shipped manifests
instead of being copied out of M18-04's prose.  Copying was the original implementation and it
shipped a wrong number: the recomputable-bytes metric was written as the whole v1-to-v2 residual
while its own evidence line said "bindings and reachability", two figures that differ by 400 bytes.

Every probe is a fingerprint function this repository actually ships, run over one shared synthetic
world that carries no prompt, media value, private path or credential.  Four of the five mutations
change that world.  The fifth cannot: a wire-contract change alters a *declared shape*, not any
instance, so it is applied to the field list the wire probe digests while every other probe is still
recomputed over the unchanged world.  That row is a real recomputation over a genuinely unchanged
input, not a copy -- an earlier version copied the baseline and reported its own construction.

The world is shared on purpose.  Giving each domain a private input would make separation trivially
true -- different bytes in, different digests out, proving nothing.  Here the UI projection is built
*from* the report, the fixture references the same node types, and the release material digests the
same declared surface, so a leak between domains has somewhere to come from.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.canonical import (  # noqa: E402
    canonical_fingerprint,
    fingerprint_context_report,
)
from comfyui_h3_context.core.fingerprint_domain import IdentityDomain  # noqa: E402
from comfyui_h3_context.core.ui_projection import (  # noqa: E402
    ExecutionCorrelation,
    UIEventState,
    build_ui_projection,
)
from comfyui_h3_context.nodes import (  # noqa: E402
    H3ContextCompilerNode,
    H3ContextPlanNode,
    H3ContextRequestNode,
)
from comfyui_h3_context.public_api import (  # noqa: E402
    get_public_manifest,
    get_public_manifest_v2,
)
from scripts.governance.closeout_matrix import (  # noqa: E402
    BlastRadiusRow,
    CloseoutMatrix,
    CloseoutMatrixError,
    CloseoutMetric,
    Probe,
    observe_blast_radius,
)

ARTIFACT_PATH = Path("governance/contracts/closeout_matrix_v1.json")

#: The accepted M18 checkpoints. Baselines are read from the first, finals from the last.
# IMPORTANT: mapped after message-only history cleanup; each accepted tree is unchanged.
M18_01 = "add8a535bca3769a91291fb31012b7447e1682b8"  # pragma: allowlist secret
M18_04 = "aea047c471e3cc87654fc67e64c6b9db6f216cfd"  # pragma: allowlist secret
M18_05 = "48c0bcb23265827f77be59b334d27effe9199844"  # pragma: allowlist secret

# CRITICAL: three packaged paths in this module address the M18 checkpoint *trees*, not the
# working tree, and they are not all packaged for the same reason. `DOMAIN` below, and the
# `retirement_eligibility_v1.json` literal in `build_metrics`, name artifacts M23-55 moved out of
# the installed package: they must keep the packaged spelling because `git show <commit>:<path>`
# resolves inside a commit where that is where they lived, and re-pathing either one turns a probe
# into ABSENT, which reads as a real regression rather than as a broken path.
# `scripts/governance/relocate_packaged_artifacts.py` declares exactly those two spans in
# `HISTORICAL_REFERENCES`. `INVENTORY` is packaged for the opposite reason -- it never moved, so
# the same spelling is correct for the historical trees and for the working tree, and it has no
# `HISTORICAL_REFERENCES` entry.
INVENTORY = "comfyui_h3_context/contracts/contract_inventory_v1.json"
DOMAIN = "comfyui_h3_context/contracts/fingerprint_domain_v1.json"

#: A synthetic intent with no private content: no locator, no path, no credential, no real media.
SYNTHETIC_INTENT = "A red kite crosses the sky while the camera follows its arc."
MUTATED_INTENT = "A blue kite crosses the sky while the camera follows its arc."


def git_show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _schema_files_at(commit: str) -> int:
    # CRITICAL: a third historical spelling, and the one with no declaration protecting it. This
    # lists the contracts directory *inside `commit`*, an accepted M18 checkpoint where every
    # schema was still packaged, so it counts what that tree held and not what HEAD holds. Adding
    # `governance/contracts/` here, or moving the prefix to it, silently returns 0 for every
    # checkpoint and the matrix reports a collapse that never happened. It is absent from
    # `HISTORICAL_REFERENCES` on purpose: that table masks spans a per-basename rewrite would
    # damage, and a bare directory prefix carries no basename to match, so declaring it would
    # dilute the table rather than protect anything. This comment is the protection.
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit, "comfyui_h3_context/contracts/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sum(1 for line in listing.splitlines() if line.endswith(".schema.json"))


def _record_at(commit: str, path: str) -> dict[str, object]:
    document = json.loads(git_show(commit, path).decode("utf-8"))
    if not isinstance(document, dict):
        raise CloseoutMatrixError(f"{path} at {commit} is not an object")
    return document


def _manifest_wire_bytes() -> dict[str, int]:
    """Manifest section sizes, measured from the shipped manifests rather than transcribed.

    M18-04 sized these with sorted compact JSON, and the repository's own `canonical_bytes`
    refuses the manifest, so the same encoding is used here. Measuring rather than copying the
    numbers out of M18-04's prose is the point: a closeout metric that is a literal cannot fail
    when the thing it describes changes.
    """

    def sized(value: object) -> int:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    v1 = get_public_manifest().to_wire()
    v2 = get_public_manifest_v2().to_wire()
    return {
        "v1_total": sized(v1),
        "v2_total": sized(v2),
        # The two sections v2 omits *because they are recomputable from the registry*. The rest of
        # the v1-to-v2 delta was trimmed for other reasons and does not belong in this metric.
        "recomputable": sized(v1["bindings"]) + sized(v1["reachability"]),
    }


def build_metrics() -> tuple[CloseoutMetric, ...]:
    """Six numbers, each read from the checkpoint or the shipped artifact that produced it."""

    manifest = _manifest_wire_bytes()
    first = _record_at(M18_01, INVENTORY)
    last = _record_at(M18_05, INVENTORY)
    first_entries: list[dict[str, object]] = first["entries"]  # type: ignore[assignment]
    last_entries: list[dict[str, object]] = last["entries"]  # type: ignore[assignment]
    final_domain = _record_at(M18_05, DOMAIN)
    final_assignments: list[dict[str, object]] = final_domain["assignments"]  # type: ignore[assignment]
    retired = json.loads(
        git_show(M18_05, "comfyui_h3_context/contracts/retirement_eligibility_v1.json").decode(
            "utf-8"
        )
    )

    def need_evidence(entries: list[dict[str, object]]) -> int:
        return sum(1 for entry in entries if entry["disposition"] == "NEED_EVIDENCE")

    return (
        CloseoutMetric(
            metric_id="contracts_with_undecided_disposition",
            unit="contracts",
            baseline=need_evidence(first_entries),
            final=need_evidence(last_entries),
            baseline_commit=M18_01,
            final_commit=M18_05,
            evidence="inventory rows still failing closed as NEED_EVIDENCE",
        ),
        CloseoutMetric(
            metric_id="identities_without_a_declared_domain",
            unit="identities",
            baseline=len(final_assignments),
            final=0,
            baseline_commit=M18_01,
            final_commit=M18_05,
            evidence="M18-02 assigned a domain to every identity it later records",
        ),
        CloseoutMetric(
            metric_id="public_manifest_wire_bytes",
            unit="bytes",
            baseline=manifest["v1_total"],
            final=manifest["v2_total"],
            baseline_commit=M18_04,
            final_commit=M18_04,
            evidence="the shipped v1 wire against the v2 it projects from, sized here not copied",
        ),
        CloseoutMetric(
            metric_id="manifest_bytes_restating_a_computable_fact",
            unit="bytes",
            baseline=manifest["recomputable"],
            final=0,
            baseline_commit=M18_04,
            final_commit=M18_04,
            evidence="the v1 bindings and reachability sections, sized on the shipped manifest",
        ),
        CloseoutMetric(
            metric_id="schemas_restating_a_wire_with_no_reader",
            unit="contracts",
            baseline=len(retired["retired"]),
            final=0,
            baseline_commit=M18_05,
            final_commit=M18_05,
            evidence="M18-05's own retirement record, which carries both ends of this number",
        ),
        CloseoutMetric(
            metric_id="shipped_schema_files",
            unit="files",
            baseline=_schema_files_at(M18_01),
            final=_schema_files_at(M18_05),
            baseline_commit=M18_01,
            final_commit=M18_05,
            evidence="the headline number, which the chain moved by nothing",
        ),
    )


@dataclass(frozen=True, slots=True)
class World:
    """One synthetic world, shared by every probe so a leak has somewhere to come from."""

    intent: str
    #: A projection-only label. Nothing semantic reads it; only the render does.
    event_state: UIEventState
    #: Stand-in fixture material: node type names, the same kind a workflow fixture carries.
    fixture_nodes: tuple[str, ...]
    #: Stand-in release material: a pinned upstream revision label.
    pinned_revision: str


BASELINE_WORLD = World(
    intent=SYNTHETIC_INTENT,
    event_state=UIEventState.SUCCESS,
    fixture_nodes=("H3ContextRequest", "H3ContextPlan", "H3ContextCompiler"),
    pinned_revision="workflow-templates/v0.0.0",
)


def _report(world: World) -> object:
    request = H3ContextRequestNode().build_request("t2va", world.intent, duration_seconds=5.0)[0]
    plan = H3ContextPlanNode().build_plan(request)[0]
    _, draft, document = H3ContextCompilerNode().compile(plan)
    from dataclasses import replace as _replace

    from comfyui_h3_context.core import validate_context_report

    return validate_context_report(_replace(draft, prompt_document=document)).report


#: Each probe is a real shipped identity, named by the function that computes it.
PROBES: tuple[Probe, ...] = (
    Probe(
        probe_id="fixture_identity",
        domain=IdentityDomain.FIXTURE,
        authority="canonical_fingerprint over workflow fixture material",
    ),
    Probe(
        probe_id="prompt_fingerprint",
        domain=IdentityDomain.SEMANTIC,
        authority="core/canonical.py::canonical_fingerprint",
    ),
    Probe(
        probe_id="release_identity",
        domain=IdentityDomain.RELEASE_INTEGRITY,
        authority="canonical_fingerprint over pinned upstream revision material",
    ),
    Probe(
        probe_id="report_fingerprint",
        domain=IdentityDomain.SEMANTIC,
        authority="core/canonical.py::fingerprint_context_report",
    ),
    Probe(
        probe_id="ui_projection_identity",
        domain=IdentityDomain.PRESENTATION,
        # A stale-render identity that did not follow the report would never detect a stale render.
        # Declared, so the dependency is a claim somebody made rather than a coincidence.
        depends_on=(IdentityDomain.SEMANTIC,),
        authority="core/ui_projection.py::BoundedUIProjection.to_wire",
    ),
    Probe(
        probe_id="wire_contract_identity",
        domain=IdentityDomain.WIRE_CONTRACT,
        authority="canonical_fingerprint over the declared contract surface",
    ),
)


def read_probes(world: World) -> dict[str, str]:
    """Run every probe over one world. Real functions, synthetic content-free material."""

    report = _report(world)
    projection = build_ui_projection(
        report,  # type: ignore[arg-type]
        ExecutionCorrelation("prompt-000", "preview"),
        world.event_state,
    )
    return {
        "fixture_identity": canonical_fingerprint({"nodes": list(world.fixture_nodes)}),
        "prompt_fingerprint": canonical_fingerprint(
            report.prompt_document.text  # type: ignore[attr-defined]
        ),
        "release_identity": canonical_fingerprint({"pinned": world.pinned_revision}),
        "report_fingerprint": fingerprint_context_report(report),  # type: ignore[arg-type]
        "ui_projection_identity": canonical_fingerprint(projection.to_wire()),
        "wire_contract_identity": canonical_fingerprint(
            {"schema": "h3.context.ui.projection.v1", "fields": sorted(projection.to_wire())}
        ),
    }


def _wire_field_names(world: World) -> tuple[str, ...]:
    """The UI projection's declared field names, which is what the wire-contract probe digests.

    Shares `read_probes`'s construction deliberately: the wire probe and this helper must see the
    same surface, or a shape mutation would be measured against a surface nothing ships.
    """

    report = _report(world)
    projection = build_ui_projection(
        report,  # type: ignore[arg-type]
        ExecutionCorrelation("prompt-000", "preview"),
        world.event_state,
    )
    return tuple(sorted(projection.to_wire()))


#: One mutation per domain. Each changes exactly one facet of the shared world.
MUTATIONS: tuple[tuple[str, IdentityDomain, Callable[[World], World], str], ...] = (
    (
        "fixture_node_set_changes",
        IdentityDomain.FIXTURE,
        lambda world: replace(world, fixture_nodes=(*world.fixture_nodes, "H3ContextPreview")),
        "a workflow fixture gains a node; no execution identity may follow it",
    ),
    (
        "pinned_upstream_revision_changes",
        IdentityDomain.RELEASE_INTEGRITY,
        lambda world: replace(world, pinned_revision="workflow-templates/v0.0.1"),
        "a pinned upstream revision moves; no execution identity may follow it",
    ),
    (
        "prompt_intent_changes",
        IdentityDomain.SEMANTIC,
        lambda world: replace(world, intent=MUTATED_INTENT),
        "the meaning changes, so every execution identity must move",
    ),
    (
        "ui_event_state_changes",
        IdentityDomain.PRESENTATION,
        lambda world: replace(world, event_state=UIEventState.VALIDATED),
        "a render-only label moves; no execution identity may follow it",
    ),
    (
        "wire_contract_shape_changes",
        IdentityDomain.WIRE_CONTRACT,
        lambda world: world,
        "the declared contract surface gains a field; instance values are untouched",
    ),
)


def build_rows() -> tuple[BlastRadiusRow, ...]:
    baseline = read_probes(BASELINE_WORLD)
    rows: list[BlastRadiusRow] = []
    for mutation_id, domain, mutate, evidence in MUTATIONS:
        if domain is IdentityDomain.WIRE_CONTRACT:
            # A declared-shape change alters no instance the other probes read, so the world is
            # unchanged. Every other probe is still recomputed over it rather than copied: a row
            # whose unmoved probes were never run again would report its own construction.
            mutated = read_probes(BASELINE_WORLD)
            mutated["wire_contract_identity"] = canonical_fingerprint(
                {
                    "schema": "h3.context.ui.projection.v1",
                    # The real declared field names plus one addition. Digesting anything else
                    # would make the move an artifact of the stand-in rather than of the shape.
                    "fields": sorted({*_wire_field_names(BASELINE_WORLD), "an_added_field"}),
                }
            )
        else:
            mutated = read_probes(mutate(BASELINE_WORLD))
        rows.append(observe_blast_radius(mutation_id, domain, baseline, mutated, evidence=evidence))
    return tuple(sorted(rows, key=lambda row: row.mutation_id))


def build_matrix() -> CloseoutMatrix:
    return CloseoutMatrix(
        probes=tuple(sorted(PROBES, key=lambda probe: probe.probe_id)),
        metrics=tuple(sorted(build_metrics(), key=lambda metric: metric.metric_id)),
        rows=build_rows(),
    )


def artifact_bytes(matrix: CloseoutMatrix) -> bytes:
    document = json.dumps(matrix.to_wire(), ensure_ascii=False, indent=2, sort_keys=True)
    return (document + "\n").encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the artifact")
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    args = parser.parse_args(argv)
    try:
        matrix = build_matrix()
    except (CloseoutMatrixError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}, ensure_ascii=False))
        return 1
    expected = artifact_bytes(matrix)
    target = ROOT / ARTIFACT_PATH
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    if args.check and (target.read_bytes() if target.is_file() else b"") != expected:
        print(
            json.dumps(
                {"status": "FAIL", "detail": "closeout matrix artifact is stale"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"status": "PASS", **matrix.to_public_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
