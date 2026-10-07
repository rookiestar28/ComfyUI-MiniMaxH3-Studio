"""M20-09 focused tests: seam filter-graph compilation and argv discipline."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_av_seam_policy import _base, _blend_spec

import comfyui_h3_context.adapters.av_reconstruction_media as media_module
from comfyui_h3_context.adapters.av_reconstruction_media import (
    AVMediaAdapterError,
    QualifiedAVMediaAdapter,
    _exact_seam_seconds,
    _seam_filter_graph,
)
from comfyui_h3_context.adapters.media_subprocess import create_output_lease
from comfyui_h3_context.core.av_seam_policy import (
    AVSeamAudioPolicy,
    AVSeamOperation,
    AVSeamPlan,
    AVSeamSpec,
    build_av_seam_plan,
)


@contextmanager
def _accepted_pin(_path: Path, _expected_sha256: str) -> Iterator[None]:
    yield


def _synthetic_adapter(root: Path, monkeypatch: pytest.MonkeyPatch) -> QualifiedAVMediaAdapter:
    ffmpeg = (root / "ffmpeg.exe").resolve()
    ffprobe = (root / "ffprobe.exe").resolve()
    ffmpeg.write_bytes(b"synthetic ffmpeg")
    ffprobe.write_bytes(b"synthetic ffprobe")
    monkeypatch.setattr(media_module, "_pin_exact_executable", _accepted_pin)
    return QualifiedAVMediaAdapter(
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        scratch_root=(root / "scratch").resolve(),
        clock_ms=lambda: 100,
    )


def _seam_plan(specs: tuple[AVSeamSpec, ...], *, count: int = 2) -> AVSeamPlan:
    plan, approval = _base(count=count, middle_frames=60 if count == 3 else None)
    return build_av_seam_plan(
        base_plan=plan,
        base_approval=approval,
        seam_specs=specs,
        planned_at_ms=320,
    )


def test_exact_seam_seconds_renders_only_grid_exact_decimals() -> None:
    assert _exact_seam_seconds(15) == "0.5"
    assert _exact_seam_seconds(30) == "1"
    assert _exact_seam_seconds(45) == "1.5"
    assert _exact_seam_seconds(150) == "5"
    with pytest.raises(AVMediaAdapterError, match="seam_duration_not_exact"):
        _exact_seam_seconds(10)


def test_blend_crossfade_graph_is_the_exact_expected_string() -> None:
    seam_plan = _seam_plan((_blend_spec(15),))
    expected = ";".join(
        (
            "[0:v:0]trim=end_frame=30,setpts=N/(30*TB)[vi0]",
            "[0:a:0]atrim=end_sample=48000,asetpts=N/SR/TB[ai0]",
            "[1:v:0]trim=end_frame=30,setpts=N/(30*TB)[vi1]",
            "[1:a:0]atrim=end_sample=48000,asetpts=N/SR/TB[ai1]",
            "[vi0]split=2[vpa0][vpb0]",
            "[vpa0]trim=end_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vex0]",
            "[vpb0]trim=start_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vtl0]",
            "[vi1]split=2[vna0][vnb0]",
            "[vna0]trim=end_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vhd0]",
            "[vnb0]trim=start_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vrs0]",
            "[vtl0][vhd0]xfade=transition=fade:duration=0.5:offset=0[vbl0]",
            "[vex0][vbl0][vrs0]concat=n=3:v=1:a=0[vj0]",
            "[ai0]asplit=2[apa0][apb0]",
            "[apa0]atrim=end_sample=24000,asetpts=PTS-STARTPTS[aex0]",
            "[apb0]atrim=start_sample=24000,asetpts=PTS-STARTPTS[atl0]",
            "[ai1]asplit=2[ana0][anb0]",
            "[ana0]atrim=end_sample=24000,asetpts=PTS-STARTPTS[ahd0]",
            "[anb0]atrim=start_sample=24000,asetpts=PTS-STARTPTS[ars0]",
            "[atl0][ahd0]acrossfade=d=0.5:c1=tri:c2=tri[abl0]",
            "[aex0][abl0][ars0]concat=n=3:v=0:a=1[aj0]",
            "[vj0]trim=end_frame=45,setpts=PTS-STARTPTS[v]",
            "[aj0]atrim=end_sample=72000,asetpts=PTS-STARTPTS[a]",
        )
    )
    assert _seam_filter_graph(seam_plan) == expected


def test_hold_policy_graphs_drop_the_exact_non_owning_samples() -> None:
    predecessor_plan = _seam_plan(
        (
            AVSeamSpec(
                operation=AVSeamOperation.CROSSFADE,
                overlap_frames=15,
                audio_policy=AVSeamAudioPolicy.PREDECESSOR,
                predecessor_master_source_id="master.left",
                successor_master_source_id="master.right",
            ),
        )
    )
    graph = _seam_filter_graph(predecessor_plan)
    assert "[ai1]atrim=start_sample=24000,asetpts=PTS-STARTPTS[ard0]" in graph
    assert "[ai0][ard0]concat=n=2:v=0:a=1[aj0]" in graph
    assert "acrossfade" not in graph
    successor_plan = _seam_plan(
        (
            AVSeamSpec(
                operation=AVSeamOperation.CROSSFADE,
                overlap_frames=15,
                audio_policy=AVSeamAudioPolicy.SUCCESSOR,
                predecessor_master_source_id="master.left",
                successor_master_source_id="master.right",
            ),
        )
    )
    graph = _seam_filter_graph(successor_plan)
    assert "[ai0]atrim=end_sample=24000,asetpts=PTS-STARTPTS[atr0]" in graph
    assert "[atr0][ai1]concat=n=2:v=0:a=1[aj0]" in graph
    assert "acrossfade" not in graph


def test_mixed_direct_and_crossfade_graph_accumulates_exactly() -> None:
    seam_plan = _seam_plan(
        (
            AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
            _blend_spec(15),
        ),
        count=3,
    )
    graph = _seam_filter_graph(seam_plan)
    assert "[vi0][vi1]concat=n=2:v=1:a=0[vj0]" in graph
    # Boundary 1 consumes the accumulated 90-frame stream's 15-frame tail.
    assert "[vj0]split=2[vpa1][vpb1]" in graph
    assert "[vpa1]trim=end_frame=75,settb=AVTB,setpts=PTS-STARTPTS[vex1]" in graph
    assert "[vpb1]trim=start_frame=75,settb=AVTB,setpts=PTS-STARTPTS[vtl1]" in graph
    assert "[vna1]trim=end_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vhd1]" in graph
    assert "[vnb1]trim=start_frame=15,settb=AVTB,setpts=PTS-STARTPTS[vrs1]" in graph
    assert "[apa1]atrim=end_sample=120000,asetpts=PTS-STARTPTS[aex1]" in graph
    assert graph.endswith(
        "[vj1]trim=end_frame=105,setpts=PTS-STARTPTS[v];"
        "[aj1]atrim=end_sample=168000,asetpts=PTS-STARTPTS[a]"
    )


def test_graph_grammar_carries_no_locator_or_foreign_text() -> None:
    plans = (
        _seam_plan((_blend_spec(15),)),
        _seam_plan(
            (
                AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
                AVSeamSpec(
                    operation=AVSeamOperation.CROSSFADE,
                    overlap_frames=15,
                    audio_policy=AVSeamAudioPolicy.SUCCESSOR,
                    predecessor_master_source_id="master.left",
                    successor_master_source_id="master.right",
                ),
            ),
            count=3,
        ),
        _seam_plan(
            (
                AVSeamSpec(
                    operation=AVSeamOperation.CROSSFADE,
                    overlap_frames=15,
                    audio_policy=AVSeamAudioPolicy.PREDECESSOR,
                    predecessor_master_source_id="master.left",
                    successor_master_source_id="master.right",
                ),
                AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),
            ),
            count=3,
        ),
    )
    allowed = re.compile(r"[A-Za-z0-9\[\]:;=,._+-]+\Z")
    for seam_plan in plans:
        graph = _seam_filter_graph(seam_plan)
        without_cadence = graph.replace("N/(30*TB)", "0").replace("N/SR/TB", "0")
        assert allowed.fullmatch(without_cadence), graph
        assert "/" not in without_cadence
        assert "\\" not in graph


def test_all_direct_seam_invocation_equals_the_legacy_aggregate_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam_plan = _seam_plan((AVSeamSpec(operation=AVSeamOperation.DIRECT_JOIN),))
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        adapter = _synthetic_adapter(root, monkeypatch)
        output_root = root / "outputs"
        output_root.mkdir()
        inputs = (root / "a.mp4", root / "b.mp4")
        lease = create_output_lease(output_root, suffix=".mp4")
        try:
            seam_invocation = adapter._seam_aggregate_invocation(
                inputs,
                lease,
                1024 * 1024,
                seam_plan=seam_plan,
            )
            legacy_invocation = adapter._aggregate_invocation(
                inputs,
                lease,
                1024 * 1024,
                expected_frames=seam_plan.total_output_frames,
                expected_samples=seam_plan.total_output_samples,
                segment_frames=seam_plan.segment_emitted_frames,
                segment_samples=seam_plan.segment_emitted_samples,
            )
            assert seam_invocation.argv == legacy_invocation.argv
            assert seam_invocation.format_whitelist == legacy_invocation.format_whitelist
            assert seam_invocation.codec_whitelist == legacy_invocation.codec_whitelist
        finally:
            lease.release()


def test_crossfade_seam_invocation_carries_the_compiled_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam_plan = _seam_plan((_blend_spec(15),))
    workspace_tmp = (Path.cwd() / ".tmp").resolve()
    workspace_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace_tmp) as temporary:
        root = Path(temporary).resolve()
        adapter = _synthetic_adapter(root, monkeypatch)
        output_root = root / "outputs"
        output_root.mkdir()
        inputs = (root / "a.mp4", root / "b.mp4")
        lease = create_output_lease(output_root, suffix=".mp4")
        try:
            invocation = adapter._seam_aggregate_invocation(
                inputs,
                lease,
                1024 * 1024,
                seam_plan=seam_plan,
            )
            argv = invocation.argv
            graph_index = argv.index("-filter_complex") + 1
            assert argv[graph_index] == _seam_filter_graph(seam_plan)
            assert argv.count("-i") == 2
            assert argv[-1] == str(lease.path)
            assert "-n" in argv
            with pytest.raises(AVMediaAdapterError, match="seam_graph_invalid"):
                adapter._seam_aggregate_invocation(
                    (inputs[0],),
                    lease,
                    1024 * 1024,
                    seam_plan=seam_plan,
                )
        finally:
            lease.release()
