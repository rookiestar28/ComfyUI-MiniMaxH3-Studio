"""Closed graph lowering; actual codec qualification lives in the explicit native lane."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_m25_authoring_render_executor import process_control, stores
from test_m25_authoring_render_jobs import _plan, long_plan
from test_m25_render_job_leases import image_bound as image_bound


def graphs() -> Any:
    return importlib.import_module("comfyui_h3_context.adapters.authoring_render_graph")


def test_schedule_preserves_every_resolved_source_and_stack_position() -> None:
    plan = _plan()
    schedule = graphs().compile_render_schedule(plan)
    assert schedule.frame_count == plan.output_profile.duration_frames
    for chunk in plan.resolved_operations:
        for frame in chunk.frames:
            active = [row for row in schedule.layers if row.start <= frame.frame < row.end]
            assert [row.layer.clip_id for row in active] == [
                layer.clip_id for layer in frame.layers
            ]
            for row, layer in zip(active, frame.layers, strict=True):
                offset = frame.frame - row.start
                assert row.source_frames[offset] == layer.source_frame
                assert row.source_pts[offset] == layer.source_pts


def test_schedule_preserves_complete_extent_without_flat_canonical_arrays() -> None:
    schedule = graphs().compile_render_schedule(long_plan(continuous_image=True))
    assert schedule.frame_count == 3600
    assert max(row.end for row in schedule.layers) == 3600


@pytest.mark.parametrize("fault", ["missing_frame", "repeated_frame", "altered_chunk"])
def test_schedule_refuses_incomplete_or_forged_frame_program(fault: str) -> None:
    plan = _plan()
    chunk = plan.resolved_operations[0]
    frames = chunk.frames
    if fault == "missing_frame":
        frames = frames[1:]
    elif fault == "repeated_frame":
        frames = (frames[0], frames[0], *frames[2:])
    else:
        chunk = replace(chunk, chunk_fingerprint="sha256:" + "0" * 64)
    changed = replace(plan, resolved_operations=(replace(chunk, frames=frames),))
    with pytest.raises(graphs().RenderGraphError, match="plan_mismatch"):
        graphs().compile_render_schedule(changed)


def test_audio_schedule_expands_back_to_exact_owned_spans_and_silence() -> None:
    plan = long_plan()
    schedule = graphs().compile_render_schedule(plan)
    assert schedule.emits_audio_stream is plan.emits_audio_stream
    for chunk in plan.resolved_operations:
        for frame in chunk.frames:
            run = next(row for row in schedule.audio if row.start <= frame.frame * 2000 < row.end)
            if frame.audio_span is None:
                assert run.asset_id is None
            else:
                span = frame.audio_span
                assert (run.asset_id, run.clip_id) == (span.asset_id, span.clip_id)
                assert (
                    run.source_start + span.output_start_sample - run.start
                    == span.source_start_sample
                )


def test_video_layer_tail_hold_follows_the_last_selected_position_not_the_source_rate(
    tmp_path: Path,
) -> None:
    """The clones that hold a layer's last source frame are stamped to follow its position.

    `tpad` stamps its clones from its own frame count times one source period, which coincides
    with the next output offset only for a source held exactly one period per frame; a source
    whose nominal rate is at least twice the output rate rounded the period to zero, the layer
    ended two frames short, and the layer below repeated the previous frame there (M25-20 B-60,
    found by the corpus's `timing.vfr_unequal_intervals` row). The graph re-stamps every clone
    after the last selected frame's position, one output frame apart.
    """

    from test_m25_render_job_leases import acquire, video_bound

    from comfyui_h3_context.adapters.authoring_render_executor import prepare_render_assets

    (tmp_path / "source").mkdir()
    _, receipt, bound = video_bound(tmp_path / "source")
    sources = acquire(bound)
    (tmp_path / "store").mkdir()
    store = stores().RenderOutputStore(tmp_path / "store")
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "a" * 64)
        prepared = prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        schedule = graphs().compile_render_schedule(bound.plan)
        graph = graphs().build_render_graph(plan=bound.plan, prepared=prepared).filter_graph
        video_runs = [
            run
            for run in schedule.layers
            if run.layer.asset_id is not None and any(f is not None for f in run.source_frames)
        ]
        assert video_runs, "the bound plan carries no video layer to hold"
        for run in video_runs:
            selected: list[tuple[int, int]] = []
            for offset, source_frame in enumerate(run.source_frames):
                if not selected or selected[-1][0] != source_frame:
                    selected.append((int(source_frame), offset))
            hold = f"if(lt(N,{len(selected)}),PTS,{selected[-1][1]}+N-{len(selected)}+1)"
            assert f"tpad=stop_mode=clone:stop=1,setpts='{hold}',fps=fps=24" in graph, (
                run.layer.clip_id
            )
    finally:
        sources.release()
        receipt.release()
        store.close()


def test_normal_layers_do_not_expand_into_full_canvas_color_and_mask_streams(
    tmp_path: Path,
) -> None:
    from test_m25_render_job_leases import acquire, video_bound

    from comfyui_h3_context.adapters.authoring_render_executor import prepare_render_assets

    source_root = tmp_path / "source"
    source_root.mkdir()
    _path, receipt, bound = video_bound(source_root)
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "b" * 32, "sha256:" + "b" * 64)
        prepared = prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        schedule = graphs().compile_render_schedule(bound.plan)
        graph = graphs().build_render_graph(plan=bound.plan, prepared=prepared).filter_graph
        normal_runs = [row for row in schedule.layers if row.layer.blend == "normal"]
        assert normal_runs, "the bound plan carries no normal-blend layer"
        for index, run in enumerate(schedule.layers):
            if run.layer.blend != "normal":
                continue
            assert f"[base{index}][l{index}timeline]overlay=" in graph
            assert f"[l{index}blank]" not in graph
            assert f"[l{index}alpha]" not in graph
            assert f"[base{index}][l{index}color][l{index}mask]maskedmerge" not in graph
    finally:
        sources.release()
        receipt.release()
        store.close()


def test_image_graph_is_closed_and_carries_only_private_generated_basenames(
    tmp_path: Path, image_bound: Any
) -> None:
    from test_m25_render_job_leases import acquire

    from comfyui_h3_context.adapters.authoring_render_executor import prepare_render_assets

    bound = image_bound[5]
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "a" * 64)
        prepared = prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        program = graphs().build_render_graph(plan=bound.plan, prepared=prepared)
        assert program.arguments[-1] == "output.mp4"
        assert str(tmp_path) not in repr(program)
        assert str(tmp_path) not in " ".join(program.arguments)
        assert prepared.media[0].path.name in program.arguments
        assert program.filter_graph.endswith("[final_v]")
        assert "-an" in program.arguments
    finally:
        sources.release()
        store.close()
