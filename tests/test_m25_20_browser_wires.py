"""M25-20 post-closeout corrective: what the browser journey is told to look at.

`scripts/nle_semantic_browser_wires.py` hands each `render_and_browser` row a prescription -- the
frames to visit and, at each, the points, identity cells, geometry targets and ramp targets to
measure -- derived from the row's composition by the same oracle functions the render stage's
extractor is prescribed from. The journey measures exactly what it is told and nothing it finds on
its own, so the prescription has to name every frame every claim needs, and each identity read has
to say which clip's element reports the source time.

Nothing here opens a browser: the prescriptions are pure functions of the composition.
"""

from __future__ import annotations

import sys
from pathlib import Path

from comfyui_h3_context.core.semantic_conformance_cases import build_recipes
from comfyui_h3_context.core.semantic_conformance_expect import (
    alpha_targets,
    derive_alphas,
    derive_source_mapping,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import nle_semantic_browser_wires as wires  # noqa: E402

BOOK = build_recipes()


def _wire(case_id: str) -> dict[str, object]:
    return wires.build_case_wire(wires._load_base_wire(), BOOK.by_id[case_id])


def test_the_prescription_visits_every_frame_a_ramp_is_read_at() -> None:
    """A ramp's alpha needs its two reference frames and every reported frame in the visit list.

    The journey computes the ramp's progress from pixels it sampled at the ramp's first included
    frame and the frame past it; a prescription that named the reported frames but not the
    references would leave the journey unable to state any alpha, and the join would block the
    row on `browser.alphas`.
    """

    wire = _wire("transition.alpha_progression")
    prescription = wires.build_prescription(wire)
    assert prescription["alpha_targets"], "the row declares a ramp"
    frames = set(prescription["frames"])
    for target in prescription["alpha_targets"]:
        assert target["base_frame"] in frames, target
        assert target["steady_frame"] in frames, target
        assert set(target["sample_frames"]) <= frames, target
    stated = {(item.label, item.output_frame) for item in derive_alphas(wire)}
    named = {
        (target["label"], frame)
        for target in prescription["alpha_targets"]
        for frame in target["sample_frames"]
    }
    assert stated <= named
    assert [t["label"] for t in prescription["alpha_targets"]] == [
        t.label for t in alpha_targets(wire)
    ]


def test_each_identity_read_names_the_clip_whose_element_reports_the_source_time() -> None:
    """A primary track that hands over to another asset mid-timeline names both clips.

    `embedded_audio.primary_without_audio` plays the primary for twelve frames and then a clip of
    the overlay's source on the same track. Each read carries the clip it belongs to, so the
    journey reads the clock of the element leased for that clip -- never a fixed element, and
    never the asset alone, which two clips may lease at once with different clocks.
    """

    wire = _wire("embedded_audio.primary_without_audio")
    prescription = wires.build_prescription(wire)
    reads = {read["output_frame"]: read for read in prescription["identity_reads"]}
    expected = {item.output_frame for item in derive_source_mapping(wire)}
    assert expected <= set(reads)
    assert reads[0]["clip_id"] == "clip-main"
    assert reads[0]["asset_id"] == "vid-primary"
    assert reads[12]["clip_id"] == "clip-split"
    assert reads[12]["asset_id"] == "vid-overlay"
    assert all(read["clip_id"] and read["asset_id"] for read in reads.values())
