"""The continuation route study harness: the fixed corpus, timings and each arm's graph wiring."""

from __future__ import annotations

import re

from comfyui_h3_context import sequence_continuation_canary as canary
from scripts import continuation_route_study as study

_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def test_the_base_corpus_is_the_planned_eighty_four_generations() -> None:
    items = study.base_corpus()
    assert len(items) == 84
    assert len({item.key for item in items}) == 84
    assert all(_KEY.fullmatch(item.key) and _KEY.fullmatch(item.key + "-dd") for item in items)
    assert sum(item.segment == 1 for item in items) == 6
    assert sum(item.arm == "rk12" for item in items) == 6
    assert {item.prompt for item in items if item.arm == "rk12"} == {"motion"}
    # Every successor runs after its predecessor.
    seen: set[str] = set()
    for item in items:
        predecessor = item.predecessor
        assert predecessor is None or predecessor.key in seen
        seen.add(item.key)


def test_lengths_trims_and_shown_extents() -> None:
    first = study.Generation("motion", 1, "base", 1)
    assert (first.generated_frames, first.trim_frames, first.shown_frames) == (192, 0, 192)
    cut = study.Generation("motion", 1, "cut", 2)
    assert (cut.generated_frames, cut.trim_frames, cut.shown_frames) == (158, 0, 153)
    for arm in ("rg-l", "rg-h", "rk1", "rk12", "rl", "rc", "rl-taper", "rc-taper"):
        item = study.Generation("motion", 1, arm, 3)
        assert (item.generated_frames, item.trim_frames, item.shown_frames) == (192, 39, 153)
    # The lattice end of a continuation segment is its shown end: no surplus to reconcile.
    assert 192 - 39 == 153


def test_the_dialogue_line_is_stated_on_the_generated_timeline() -> None:
    continuation = study.Generation("dialogue", 1, "rl", 2).prompt_text()
    cut = study.Generation("dialogue", 1, "cut", 2).prompt_text()
    assert "[4.125s-6.625s]" in continuation
    assert "[2.5s-5s]" in cut
    # The quiet head: no timed event before trim / 24 + 0.5 s on a continuation segment.
    assert 4.125 >= 39 / 24 + 0.5
    assert "{t0}" not in study.Generation("motion", 1, "rl", 2).prompt_text()


def _classes(graph: dict[str, dict[str, object]]) -> dict[str, str]:
    return {node: str(value["class_type"]) for node, value in graph.items()}


def test_each_arm_wires_its_route_between_the_template_endpoints() -> None:
    for arm, (kind, mode) in study.ARMS.items():
        item = study.Generation("motion", 2, arm, 2)
        graph = study.build_graph(item, prefix="p")
        classes = _classes(graph)
        guider = graph["guider"]["inputs"]
        sampler = graph["run"]["inputs"]
        if kind == "latent":
            route = graph["route"]["inputs"]
            assert classes["route"] == canary.TAIL_COMPOSE_NODE_ID
            assert route["mode"] == mode and mode in canary.MODES
            assert route["key"] == "motion-2-base-1"
            assert guider["conditioning"] == ["route", 0]
            assert sampler["latent_image"] == ["route", 1]
            assert classes["capture"] == canary.LATENT_TAIL_CAPTURE_NODE_ID
        elif kind == "guide":
            assert classes["route"] == "MiniMaxH3AddGuide"
            assert graph["route"]["inputs"]["frame_idx"] == 0
            assert classes["clip"] == canary.TAIL_CLIP_NODE_ID
            expected = "motion-2-base-1" if arm == "rg-l" else "motion-2-base-1-h264"
            assert graph["clip"]["inputs"]["key"] == expected
            assert guider["conditioning"] == ["route", 0]
            assert sampler["latent_image"] == ["h3", 1]
        else:
            assert "route" not in graph
            assert guider["conditioning"] == ["h3", 0] and sampler["latent_image"] == ["h3", 1]
        assert ("window" in graph) == (arm == "rg-l")
        trim = graph["trim"]["inputs"]
        assert trim["trim_frames"] == item.trim_frames and trim["keep_frames"] == 153
        assert graph["noise"]["inputs"]["noise_seed"] == 202


def test_segment_one_captures_both_tails_and_a_second_decode() -> None:
    graph = study.build_graph(study.Generation("dialogue", 3, "base", 1), prefix="p")
    classes = _classes(graph)
    assert classes["capture"] == canary.LATENT_TAIL_CAPTURE_NODE_ID
    assert classes["window"] == canary.TAIL_WINDOW_NODE_ID
    assert graph["window"]["inputs"]["shown_frames"] == 192
    assert graph["decode_v2"]["inputs"]["samples"] == ["run", 0]
    assert graph["decode_v"]["inputs"]["samples"] == ["capture", 0]
    assert graph["trim2"]["inputs"]["retain_key"] == "dialogue-3-base-1-dd"
    third = study.build_graph(study.Generation("motion", 1, "rl", 3), prefix="p")
    assert "capture" not in third and third["route"]["inputs"]["key"] == "motion-1-rl-2"


def test_the_neutral_segment_three_variant_reuses_the_base_segments() -> None:
    item = study.Generation("motionb", 2, "rl", 3)
    assert item.predecessor == study.Generation("motion", 2, "rl", 2)
    assert "warmer" not in item.prompt_text() and "same golden-hour light" in item.prompt_text()
    assert "warmer" in study.Generation("motion", 2, "rl", 3).prompt_text()
    chain = study._chain("motionb", 2, "rl")
    assert [g.key for g in chain] == ["motion-2-base-1", "motion-2-rl-2", "motionb-2-rl-3"]
    graph = study.build_graph(item, prefix="p")
    assert graph["route"]["inputs"]["key"] == "motion-2-rl-2"
    rgl = study.build_graph(study.Generation("motionb", 2, "rg-l", 3), prefix="p")
    assert rgl["clip"]["inputs"]["key"] == "motion-2-rg-l-2"
