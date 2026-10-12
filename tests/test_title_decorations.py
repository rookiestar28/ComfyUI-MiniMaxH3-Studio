"""Literal title staging and closed decoration wire regressions."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_m25_authoring_render_executor import executors, process_control, stores
from test_m25_composition_contract import fixture_snapshot, resign
from test_m25_render_job_leases import image_bound as image_bound
from test_m25_render_planner import _snapshot

from comfyui_h3_context.adapters.authoring_fonts import load_packaged_font_manifest
from comfyui_h3_context.core.composition_contract import (
    CompositionContractError,
    decode_public_snapshot,
)


def decorated_wire() -> dict[str, Any]:
    wire = fixture_snapshot()
    wire["clips"][3]["text"].update(
        stroke={"rgba": [1, 2, 3, 128], "width_px": 32},
        shadow={"rgba": [4, 5, 6, 255], "offset_x_px": 0, "offset_y_px": 0},
        background_rgba=[7, 8, 9, 128],
        background_padding_px=64,
    )
    return resign(wire)


def test_decorations_round_trip_with_zero_offset_shadow_and_legacy_identity() -> None:
    wire = decorated_wire()
    assert decode_public_snapshot(wire).to_wire() == wire
    legacy = resign(fixture_snapshot())
    assert decode_public_snapshot(legacy).to_wire() == legacy


@pytest.mark.parametrize(
    "member,value",
    [
        ("stroke", None),
        ("stroke", {"rgba": [0, 0, 0, 0], "width_px": 1}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": 0}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": 33}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": True}),
        ("stroke", {"rgba": [0, 0, 0, 256], "width_px": 1}),
        ("stroke", {"rgba": [0, 0, 0, True], "width_px": 1}),
        ("stroke", {"rgba": [0, 0, 0], "width_px": 1}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": 1.5}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": "1"}),
        ("stroke", {"rgba": [0, 0, 0, 255]}),
        ("stroke", {"rgba": [0, 0, 0, 255], "width_px": 1, "blur": 1}),
        ("shadow", {"rgba": [0, 0, 0, 0], "offset_x_px": 0, "offset_y_px": 0}),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": -65, "offset_y_px": 0}),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": 0, "offset_y_px": 65}),
        ("shadow", None),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": True, "offset_y_px": 0}),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": 0, "offset_y_px": 1.5}),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": 0}),
        ("shadow", {"rgba": [0, 0, 0, 255], "offset_x_px": 0, "offset_y_px": 0, "blur": 1}),
        ("background_padding_px", 0),
        ("background_padding_px", 65),
        ("background_padding_px", True),
        ("background_padding_px", None),
        ("background_padding_px", 1.5),
        ("background_padding_px", "1"),
    ],
)
def test_noncanonical_decorations_are_refused(member: str, value: object) -> None:
    wire = decorated_wire()
    wire["clips"][3]["text"][member] = value
    with pytest.raises(CompositionContractError) as error:
        decode_public_snapshot(resign(wire))
    # Core canonical admission refuses JSON floats before member-specific integer checks.
    contains_float = (
        type(value) is float
        or isinstance(value, dict)
        and any(type(member) is float for member in value.values())
    )
    assert error.value.code == ("resource_limit" if contains_float else "invalid_contract")


def test_padding_requires_a_background() -> None:
    wire = decorated_wire()
    wire["clips"][3]["text"]["background_rgba"] = None
    with pytest.raises(CompositionContractError):
        decode_public_snapshot(resign(wire))


@pytest.mark.parametrize("content", ["H3\nH3", "H3\n", "H3\n\nH3\n", "\n\n", "H\n" * 32])
@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_every_title_stages_literal_lf_free_lines_and_retains_blank_bands(
    tmp_path: Path, content: str, align: str
) -> None:
    title = next(clip.text for clip in _snapshot().clips if clip.text is not None)
    title = replace(title, content=content, align=align)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "b" * 64)
        prepared = executors().prepare_render_text(
            clip_id="literal-title",
            style=title,
            fonts=load_packaged_font_manifest(),
            stage=stage,
            control=process_control(),
        )
        assert prepared.text_path.read_bytes() == content.encode()
        assert prepared.text_fingerprint == "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        assert len(prepared.lines) == len(content.split("\n"))
        for (path, digest), line in zip(prepared.lines, content.split("\n"), strict=True):
            assert (path.read_bytes() if path else b"") == line.encode()
            assert path is not None if line else path is None
            assert digest == "sha256:" + hashlib.sha256(line.encode()).hexdigest()
            if path is not None:
                assert b"\n" not in path.read_bytes()
    finally:
        store.close()


@pytest.mark.parametrize("content", ["H3\nH3", "\n\n"])
def test_decorated_graph_separates_coverage_and_holds_before_temporal_operations(
    tmp_path: Path, image_bound: Any, content: str
) -> None:
    from test_m25_render_job_leases import acquire
    from test_m25_render_planner import _snapshot_wire

    from comfyui_h3_context.adapters.authoring_render_graph import build_render_graph
    from comfyui_h3_context.adapters.authoring_render_source import prepare_bound_render_plan
    from comfyui_h3_context.core.composition_contract import public_snapshot_fingerprint

    original = image_bound[5]
    wire = original._issued_snapshot.to_wire()
    template = _snapshot_wire()
    wire["assets"].append(template["assets"][3])
    wire["tracks"].append(template["tracks"][3])
    title = template["clips"][3]
    title.update(start_frame=0, duration_frames=4)
    title["text"].update(
        content=content,
        stroke={"rgba": [0, 255, 0, 128], "width_px": 2},
        shadow={"rgba": [0, 0, 255, 128], "offset_x_px": 0, "offset_y_px": 0},
        background_rgba=[255, 0, 0, 128],
        background_padding_px=3,
    )
    wire["clips"].append(title)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    bound = prepare_bound_render_plan(original._history, decode_public_snapshot(wire), lambda: True)
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "b" * 64)
        prepared = executors().prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        graph = build_render_graph(plan=bound.plan, prepared=prepared).filter_graph
        hold = graph.index("loop=loop=-1:size=1:start=0")
        correction = graph.index(
            "alphamerge,setparams=alpha_mode=premultiplied,format=gbrap,unpremultiply=inplace=1"
        )
        assert (
            correction
            < hold
            < graph.index(",crop=", hold)
            < graph.index("colorchannelmixer=aa=", hold)
        )
        assert ",," not in graph and "shadowx=" not in graph
        assert "trim=end_frame=1,split=2" in graph
        if content == "\n\n":
            assert "drawtext=" not in graph
        else:
            for branch in graph.split(";\n"):
                if "drawtext=" not in branch:
                    continue
                passes = branch.split("drawtext=")[1:]
                assert len(passes) == 6
                assert all(":box=1:" in draw for draw in passes[:2])
                assert all(":box=1:" not in draw for draw in passes[2:])
    finally:
        sources.release()
        store.close()


@pytest.mark.parametrize("size,line_height", [(30, 7500), (18, 12500)])
@pytest.mark.parametrize("decorated", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_half_integer_line_advance_matches_canvas_positive_rounding(
    tmp_path: Path, image_bound: Any, size: int, line_height: int, decorated: bool, terminal: bool
) -> None:
    from test_m25_render_job_leases import acquire
    from test_m25_render_planner import _snapshot_wire

    from comfyui_h3_context.adapters.authoring_render_graph import build_render_graph
    from comfyui_h3_context.adapters.authoring_render_source import prepare_bound_render_plan
    from comfyui_h3_context.core.composition_contract import public_snapshot_fingerprint

    original = image_bound[5]
    wire = original._issued_snapshot.to_wire()
    template = _snapshot_wire()
    wire["assets"].append(template["assets"][3])
    wire["tracks"].append(template["tracks"][3])
    title = template["clips"][3]
    title.update(start_frame=0, duration_frames=4)
    title["text"].update(
        content="\n".join(["H3"] * 32) + ("\n" if terminal else ""),
        size_px=size,
        line_height_bp=line_height,
        background_rgba=[255, 0, 0, 255],
    )
    if decorated:
        title["text"]["shadow"] = {"rgba": [0, 0, 255, 255], "offset_x_px": 0, "offset_y_px": 0}
    wire["clips"].append(title)
    wire["public_fingerprint"] = public_snapshot_fingerprint(wire)
    bound = prepare_bound_render_plan(original._history, decode_public_snapshot(wire), lambda: True)
    sources = acquire(bound)
    store = stores().RenderOutputStore(tmp_path)
    try:
        stage = store.begin("render-" + "a" * 32, "sha256:" + "b" * 64)
        prepared = executors().prepare_render_assets(
            plan=bound.plan, sources=sources, stage=stage, control=process_control()
        )
        graph = build_render_graph(plan=bound.plan, prepared=prepared).filter_graph
        bands = 33 if terminal else 32
        for line in range(32):
            anchor = f"(h-{bands * 23})/2+{line * 23}"
            assert f":y={anchor}:box=1:" in graph
            assert f":y={anchor}+(23-font_a-font_d)/2" in graph
        assert ":boxh=22:" not in graph
        assert graph.count(":boxh=23:") == (64 if decorated else 32)
        assert f":y=(h-{bands * 23})/2+{32 * 23}:" not in graph
    finally:
        sources.release()
        store.close()


def test_decorations_survive_project_and_fresh_recovery_undo_redo() -> None:
    import copy

    from test_m25_timeline_history_v2 import transaction
    from test_project_document import document_wire

    from comfyui_h3_context.core.editor_recovery import history_data, restore_history_data
    from comfyui_h3_context.core.project_document import (
        decode_project_document,
        decode_project_value,
        editable_authoring_wire,
        encode_project_document,
    )
    from comfyui_h3_context.core.timeline_authoring import NleCommand, NleCommandKind
    from comfyui_h3_context.core.timeline_history_v2 import (
        TimelineHistoryStateV2,
        apply_timeline_transaction_v2,
    )

    wire = document_wire()
    template = decorated_wire()
    title = template["clips"][3]
    title.update(start_frame=0, duration_frames=24)
    title["text"]["content"] = "H\n" * 32
    wire["editor"]["assets"].append(template["assets"][3])
    wire["editor"]["tracks"].append(template["tracks"][3])
    wire["editor"]["clips"].append(title)
    document = decode_project_value(wire)
    assert decode_project_document(encode_project_document(document)).to_wire() == wire
    state = TimelineHistoryStateV2.initialize(
        document.fresh_authoring("project.title", "authoring-title")
    )
    before = next(clip for clip in state.authoring.clips if clip.text is not None)
    style = copy.deepcopy(title["text"])
    style.pop("content")
    style["size_px"] = 48
    state, _ = apply_timeline_transaction_v2(
        state,
        transaction(
            state,
            "title.style",
            NleCommand(NleCommandKind.SET_TEXT_STYLE, {"clip_id": before.clip_id, "style": style}),
        ),
    )
    expected = next(clip for clip in state.authoring.clips if clip.text is not None)
    assert expected.text is not None
    assert expected.text.to_wire() == {"content": "H\n" * 32, **style}
    wire["editor"].update(editable_authoring_wire(state.authoring))
    restored = restore_history_data(
        decode_project_value(wire),
        history_data(state),
        project_id="project.restored",
        workspace_handle="authoring-restored",
    )
    assert next(clip for clip in restored.authoring.clips if clip.text is not None) == expected
    undone, receipt = apply_timeline_transaction_v2(
        restored,
        transaction(
            restored,
            "title.undo",
            NleCommand(NleCommandKind.UNDO, {"history_cursor": restored.undo_entries[-1].cursor}),
        ),
    )
    assert next(clip for clip in undone.authoring.clips if clip.text is not None) == before
    redone, _ = apply_timeline_transaction_v2(
        undone,
        transaction(
            undone,
            "title.redo",
            NleCommand(NleCommandKind.REDO, {"history_cursor": receipt.history_cursor}),
        ),
    )
    assert next(clip for clip in redone.authoring.clips if clip.text is not None) == expected
