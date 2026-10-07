"""M25-20 F1 corrective, browser half: the exact composition wire for every `render_and_browser`
corpus row, for the Playwright journey that presents each one in the real browser and records the
`h3.context.nle_semantic_browser_stage.v1` observation `scripts/nle_semantic_report.py` joins.

This module only *imports* the accepted core and corpus modules; it edits nothing they own. It
does not decide expectations, does not render anything, and does not touch the render stage's own
artifact.

GUARD: the composition each row presents is resolved through `semantic_conformance_drive.run_setup`
-- the SAME function `scripts/nle_semantic_report.py`'s own `_composition` calls to know what a row
actually rendered, and the same one the qualification stage drives a row's setup through. An earlier
version of this script re-implemented that resolution by hand (its own `_resolve_payload`/
`_transact`, plus a video-landmark "densification" patch to route around a
`source_range_unavailable` refusal). That divergence was a mistake, not a workaround: it meant the
composition presented in the browser was not provably the composition the join judges the row
against, and the "exact landmark lookup" refusal it was routing around is exactly the same refusal
`run_setup` raises for the 3 rows that are genuinely `BLOCKED` (an edge-trim direction and an
embedded-audio roll whose owner range does not exist in the base fixture's clip layout) -- a
real, structural limitation, not a densification gap. Never reintroduce a private resolution path
here; call `run_setup`/`initial_state` and let them raise exactly what the real engine raises.

Usage:
    python scripts/nle_semantic_browser_wires.py --output <path>

Writes one JSON document: `{"schema": "h3.context.nle_semantic_browser_wires.v2", "rows": [...]}`,
each row `{"case_id": ..., "status": "READY", "wire": {...}, "prescription": {...}}` or, if a
row's setup cannot be resolved, `{"case_id": ..., "status": "BLOCKED", "blocked_code": "..."}` --
never a silently omitted row, matching TEST_SOP 3.6's "a stage that cannot reach a row reports
BLOCKED or NOT_RUN".

The `prescription` is where the browser observer must look, derived from the row's composition by
the same functions the render stage hands the independent extractor
(`semantic_conformance_expect.patch_sample_points` / `identity_reads` / `geometry_targets`): the
canvas points to sample at each frame, the identity cells to read at each frame with the asset's
declared landmark table, and the layers to locate at their own sample frame. GUARD: the browser
observer measures at these points and nowhere else. An earlier version located every mark by
searching the canvas for its colour and solved the identity row's position from the marks it
found, which reads the right index out of a misplaced layer -- the geometry landmark's question,
answered by the wrong observer -- and measured a scaled overlay's mark wherever its colour
happened to be. Never reintroduce a colour search here or in the TypeScript helper.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfyui_h3_context.core.errors import ContractValidationError  # noqa: E402
from comfyui_h3_context.core.semantic_conformance_cases import (  # noqa: E402
    BASE_FIXTURE_NAMES,
    BASE_PRESENTATION,
    CORPUS_BASE,
    CaseRecipe,
    build_recipes,
)
from comfyui_h3_context.core.semantic_conformance_drive import (  # noqa: E402
    initial_state,
    resolve_payload,
    transact,
)
from comfyui_h3_context.core.semantic_conformance_expect import (  # noqa: E402
    alpha_targets,
    browser_observable,
    geometry_targets,
    identity_reads,
    patch_sample_points,
    preview_scale,
)

#: GUARD: the M25-20 corpus base, the same document `scripts/nle_semantic_report.py` resolves every
#: row's judged composition against and `scripts/nle_semantic_render.py` renders from. The M25-10
#: contract fixture it replaced declares a different source landmark table and audio binding for
#: the same assets, so a row presented from it could be `source_range_unavailable` here while the
#: join judged a composition that resolved -- the browser stage then reported a row blocked that
#: the render stage observed, for a difference that was never the product's.
FIXTURES_DIR = ROOT / "tests" / "fixtures"
FIXTURE_PATH = FIXTURES_DIR / BASE_FIXTURE_NAMES[CORPUS_BASE]
SCHEMA = "h3.context.nle_semantic_browser_wires.v2"


class WireBuildError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: The workspace identity the frontend E2E harness's own bootstrap uses for every shape
#: (`frontend/tests/support/nleWorkspaceFixture.ts`'s `FIXTURE_WORKSPACE_HANDLE`). The
#: authoring session refuses a `read_timeline_history` response naming a different workspace
#: handle than the one its own session was established with (`cross_workspace_response` --
#: `authoringWorkbenchCodec.ts`'s cross-workspace guard), so a row presented in the browser must
#: carry this exact handle. The value is opaque session-scoping plumbing, not part of any M25-20
#: claim (transform/crop/effect/etc. never reference it), so substituting it changes nothing the
#: corpus asserts -- it only lets the harness's already-open session accept this row's timeline.
_BROWSER_HARNESS_WORKSPACE_HANDLE = "authoring-" + "a" * 32


def _load_base_wire(base: str = CORPUS_BASE) -> dict[str, Any]:
    """The corpus's own accepted base snapshot, unclaimed, aligned to the harness's workspace.

    `scripts/nle_semantic_render.py`'s `_base_wire` substitutes real render-source-bound asset
    entries here for the sake of actually encoding video with ffmpeg; `run_setup` needs no such
    binding -- it only decodes and transacts against the timeline contract -- so the raw fixture
    document's own `snapshot` is exactly the `base` the join itself resolves a row's composition
    against (`nle_semantic_report._composition`), apart from the workspace handle substitution
    above.
    """

    document = json.loads((FIXTURES_DIR / BASE_FIXTURE_NAMES[base]).read_text(encoding="utf-8"))
    wire = dict(document["snapshot"])
    wire["workspace_handle"] = _BROWSER_HARNESS_WORKSPACE_HANDLE
    return wire


def build_case_wire(base_wire: Any, recipe: CaseRecipe) -> dict[str, Any]:
    """The exact final wire a `render_and_browser` row presents: edits, then every setup command.

    `render_and_browser` recipes never carry a `command` (`CaseRecipe.__post_init__` enforces
    this), so the state reached after edits and every declared setup command -- with no "before"/
    "after" split -- is the one and only composition the row is about.

    GUARD: this loop mirrors `semantic_conformance_drive.run_setup` command-for-command, using its
    own exported `initial_state`/`resolve_payload`/`transact` primitives rather than any private
    substitute, so the composition reached here can never diverge from the one the join itself
    resolves the row against. It exists only because `run_setup` swallows the underlying
    `ContractValidationError`'s `code` into a generic message, and this script's BLOCKED rows are
    worth reporting with the engine's own refusal code rather than that generic string.
    """

    state = initial_state(recipe, base_wire)
    cursor: str | None = None
    for index, step in enumerate(recipe.setup):
        payload = resolve_payload(dict(step.payload), cursor, state.snapshot.timeline_fingerprint)
        state, receipt = transact(state, step.kind, payload, f"{recipe.case_id}-setup-{index}")
        cursor = receipt.history_cursor
    return dict(state.snapshot.to_wire())


def _presentation(wire: dict[str, Any], base: str) -> dict[str, float]:
    """The picture box, ratio and backing this base is presented in, stated for the journey.

    GUARD: the journey reads these numbers rather than holding its own copy. A second table in
    TypeScript would not fail loudly when the two disagreed: the stage would prescribe landmarks
    for a presentation the browser never arranged, and the rows would be observed at a size their
    expectations were not written for.
    """

    declared = BASE_PRESENTATION[base]
    output = wire["output"]
    ratio = float(declared["device_pixel_ratio"])
    return {
        "pane_css_width": float(declared["pane_css_width"]),
        "pane_css_height": float(declared["pane_css_height"]),
        "device_pixel_ratio": ratio,
        # What the monitor must actually produce. Zero means "whatever the default layout gives",
        # which is the accepted base: it is inside the legacy floor at any picture box.
        "backing_width": int(output["width"]) if declared["pane_css_width"] else 0,
        "backing_height": int(output["height"]) if declared["pane_css_width"] else 0,
    }


def build_prescription(wire: dict[str, Any], base: str = CORPUS_BASE) -> dict[str, Any]:
    """Where the browser observer looks for this composition, at which frames, and how presented.

    `frames` is every output frame any point, read or target names, so the journey visits each
    exactly once and nothing is sampled at a frame its own label does not name. A composition the
    product presents scaled down (`browser_observable` false) prescribes nothing: see
    `semantic_conformance_expect.BROWSER_OBSERVES_ONLY_AN_UNSCALED_PREVIEW`. Whether it does is
    decided in the presentation this row's base declares, never in the default one.
    """

    presentation = _presentation(wire, base)
    if not browser_observable(wire, base):
        return {
            "presentation": presentation,
            "preview_scale": preview_scale(wire, **BASE_PRESENTATION[base]),
            "frames": [],
            "patch_points": [],
            "identity_reads": [],
            "geometry_targets": [],
            "alpha_targets": [],
        }
    points = patch_sample_points(wire)
    reads = identity_reads(wire)
    targets = geometry_targets(wire)
    ramps = alpha_targets(wire)
    frames = sorted(
        {point.output_frame for point in points}
        | {read.output_frame for read in reads}
        | {target.sample_frame for target in targets}
        | {ramp.base_frame for ramp in ramps}
        | {ramp.steady_frame for ramp in ramps}
        | {frame for ramp in ramps for frame in ramp.sample_frames}
    )
    return {
        "presentation": presentation,
        "preview_scale": preview_scale(wire, **BASE_PRESENTATION[base]),
        "frames": frames,
        "patch_points": [
            {
                "label": point.label,
                "output_frame": point.output_frame,
                "canvas_x": point.canvas_x,
                "canvas_y": point.canvas_y,
            }
            for point in points
        ],
        "identity_reads": [
            {
                "output_frame": read.output_frame,
                "clip_id": read.clip_id,
                "asset_id": read.asset_id,
                "cells": [[x, y] for x, y in read.cells],
                "pts_by_frame": {str(index): pts for index, pts in read.pts_by_frame.items()},
                "time_base_num": read.time_base_num,
                "time_base_den": read.time_base_den,
            }
            for read in reads
        ],
        "geometry_targets": [
            {
                "clip_id": target.clip_id,
                "asset_id": target.asset_id,
                "sample_frame": target.sample_frame,
            }
            for target in targets
        ],
        "alpha_targets": [
            {
                "label": ramp.label,
                "canvas_x": ramp.canvas_x,
                "canvas_y": ramp.canvas_y,
                "opacity_bp": ramp.opacity_bp,
                "base_frame": ramp.base_frame,
                "steady_frame": ramp.steady_frame,
                "sample_frames": list(ramp.sample_frames),
            }
            for ramp in ramps
        ],
    }


def build_all_rows() -> list[dict[str, Any]]:
    # One base wire per declared base, so a row is always built from the composition its own
    # recipe names; the join resolves the same row against the same base by the same name.
    base_wires = {name: _load_base_wire(name) for name in BASE_FIXTURE_NAMES}
    book = build_recipes()
    recipes = [recipe for recipe in book.recipes if recipe.observation == "render_and_browser"]
    rows: list[dict[str, Any]] = []
    for recipe in recipes:
        try:
            wire = build_case_wire(base_wires[recipe.base], recipe)
        except WireBuildError as exc:
            rows.append(
                {
                    "case_id": recipe.case_id,
                    "base": recipe.base,
                    "status": "BLOCKED",
                    "blocked_code": exc.code,
                }
            )
            continue
        except ContractValidationError as exc:
            # A real refusal from the accepted decoder/command engine while resolving this row's
            # declared `setup` -- never guessed at or routed around, reported with the code the
            # engine itself raised so it can be cross-checked against the render stage's own
            # BLOCKED rows for the same case_id.
            code = getattr(exc, "code", None) or type(exc).__name__
            rows.append(
                {
                    "case_id": recipe.case_id,
                    "base": recipe.base,
                    "status": "BLOCKED",
                    "blocked_code": str(code),
                }
            )
            continue
        rows.append(
            {
                "case_id": recipe.case_id,
                # Which composition base the row is a case of. The browser stage arranges its
                # viewport and device pixel ratio per base -- a 1280 x 720 backing does not fit a
                # 1440 x 900 viewport at ratio 1 -- so it has to know before it presents the row.
                "base": recipe.base,
                "status": "READY",
                "wire": wire,
                "prescription": build_prescription(wire, recipe.base),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="where to write the wires")
    args = parser.parse_args(argv)

    rows = build_all_rows()
    document = {"schema": SCHEMA, "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ready = sum(1 for row in rows if row["status"] == "READY")
    blocked = sum(1 for row in rows if row["status"] == "BLOCKED")
    print(json.dumps({"required_rows": len(rows), "ready": ready, "blocked": blocked}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
